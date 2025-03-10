#
# This file is part of pySMT.
#
#   Copyright 2014 Andrea Micheli and Marco Gario
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
import re

from warnings import warn
from fractions import Fraction
from six.moves import xrange

from pysmt.exceptions import SolverAPINotFound

try:
    import mathsat
except ImportError:
    raise SolverAPINotFound

from pysmt.logics import LRA, QF_UFLIA, QF_UFLRA, QF_BV, PYSMT_QF_LOGICS
from pysmt.oracles import get_logic

import pysmt.operators as op
from pysmt import typing as types
from pysmt.solvers.solver import (IncrementalTrackingSolver, UnsatCoreSolver,
                                  Converter)
from pysmt.solvers.smtlib import SmtLibBasicSolver, SmtLibIgnoreMixin
from pysmt.solvers.eager import EagerModel
from pysmt.walkers import DagWalker
from pysmt.exceptions import (SolverReturnedUnknownResultError,
                              SolverNotConfiguredForUnsatCoresError,
                              SolverStatusError,
                              InternalSolverError)
from pysmt.decorators import clear_pending_pop, catch_conversion_error
from pysmt.solvers.qelim import QuantifierEliminator
from pysmt.solvers.interpolation import Interpolator
from pysmt.walkers.identitydag import IdentityDagWalker


class MathSAT5Solver(IncrementalTrackingSolver, UnsatCoreSolver,
                     SmtLibBasicSolver, SmtLibIgnoreMixin):

    LOGICS = PYSMT_QF_LOGICS

    def __init__(self, environment, logic, user_options=None, debugFile=None):
        IncrementalTrackingSolver.__init__(self,
                                           environment=environment,
                                           logic=logic,
                                           user_options=user_options)

        self.msat_config = mathsat.msat_create_default_config(str(logic))
        self._prepare_config(self.options, debugFile)
        self.msat_env = mathsat.msat_create_env(self.msat_config)

        self.realType = mathsat.msat_get_rational_type(self.msat_env)
        self.intType = mathsat.msat_get_integer_type(self.msat_env)
        self.boolType = mathsat.msat_get_bool_type(self.msat_env)

        self.mgr = environment.formula_manager
        self.converter = MSatConverter(environment, self.msat_env)
        return

    def _prepare_config(self, options, debugFile=None):
        """Sets the relevant options in self.msat_config"""
        if options.generate_models:
            check = mathsat.msat_set_option(self.msat_config, "model_generation",
                                            "true")
            assert check == 0

        if options.unsat_cores_mode is not None:
            check = mathsat.msat_set_option(self.msat_config,
                                            "unsat_core_generation",
                                            "1")
            assert check == 0

        if debugFile is not None:
            mathsat.msat_set_option(self.msat_config,
                                    "debug.api_call_trace", "1")
            mathsat.msat_set_option(self.msat_config,
                                    "debug.api_call_trace_filename",
                                    debugFile)

        mathsat.msat_set_option(self.msat_config,
                                "theory.bv.div_by_zero_mode", "0")

    @clear_pending_pop
    def _reset_assertions(self):
        mathsat.msat_reset_env(self.msat_env)

    @clear_pending_pop
    def declare_variable(self, var):
        self.converter.declare_variable(var)

    @clear_pending_pop
    def _add_assertion(self, formula, named=None):
        self._assert_is_boolean(formula)

        result = formula
        if self.options.unsat_cores_mode == "named":
            # If we want named unsat cores, we need to rewrite the
            # formulae as implications
            key = self.mgr.FreshSymbol(template="_assertion_%d")
            result = (key, named, formula)
            formula = self.mgr.Implies(key, formula)

        term = self.converter.convert(formula)
        res = mathsat.msat_assert_formula(self.msat_env, term)

        if res != 0:
            msat_msg = mathsat.msat_last_error_message(self.msat_env)
            raise InternalSolverError(msat_msg)

        return result

    def _named_assertions(self):
        if self.options.unsat_cores_mode == "named":
            return [t[0] for t in self.assertions]
        return None

    def _named_assertions_map(self):
        if self.options.unsat_cores_mode == "named":
            return dict((t[0], (t[1],t[2])) for t in self.assertions)
        return None

    @clear_pending_pop
    def _solve(self, assumptions=None):
        res = None

        n_ass = self._named_assertions()
        if n_ass is not None and len(n_ass) > 0:
            if assumptions is None:
                assumptions = n_ass
            else:
                assumptions += n_ass

        if assumptions is not None:
            bool_ass = []
            other_ass = []
            for x in assumptions:
                if x.is_literal():
                    bool_ass.append(self.converter.convert(x))
                else:
                    other_ass.append(x)

            if len(other_ass) > 0:
                self.push()
                self.add_assertion(self.mgr.And(other_ass))
                self.pending_pop = True

            if len(bool_ass) > 0:
                res = mathsat.msat_solve_with_assumptions(self.msat_env, bool_ass)
            else:
                res = mathsat.msat_solve(self.msat_env)

        else:
            res = mathsat.msat_solve(self.msat_env)

        assert res in [mathsat.MSAT_UNKNOWN,mathsat.MSAT_SAT,mathsat.MSAT_UNSAT]
        if res == mathsat.MSAT_UNKNOWN:
            raise SolverReturnedUnknownResultError

        return (res == mathsat.MSAT_SAT)

    def _check_unsat_core_config(self):
        if self.options.unsat_cores_mode is None:
            raise SolverNotConfiguredForUnsatCoresError

        if self.last_result is None or self.last_result:
            raise SolverStatusError("The last call to solve() was not" \
                                    " unsatisfiable")

        if self.last_command != "solve":
            raise SolverStatusError("The solver status has been modified by a" \
                                    " '%s' command after the last call to" \
                                    " solve()" % self.last_command)

    def get_unsat_core(self):
        """After a call to solve() yielding UNSAT, returns the unsat core as a
        set of formulae"""
        if self.options.unsat_cores_mode == "all":
            self._check_unsat_core_config()

            terms = mathsat.msat_get_unsat_core(self.msat_env)
            if terms is None:
                raise InternalSolverError(
                    mathsat.msat_last_error_message(self.msat_env))
            return set(self.converter.back(t) for t in terms)
        else:
            return self.get_named_unsat_core().values()

    def get_named_unsat_core(self):
        """After a call to solve() yielding UNSAT, returns the unsat core as a
        dict of names to formulae"""
        if self.options.unsat_cores_mode == "named":
            self._check_unsat_core_config()

            assumptions = mathsat.msat_get_unsat_assumptions(self.msat_env)
            pysmt_assumptions = set(self.converter.back(t) for t in assumptions)

            res = {}
            n_ass_map = self._named_assertions_map()
            cnt = 0
            for key in pysmt_assumptions:
                if key in n_ass_map:
                    (name, formula) = n_ass_map[key]
                    if name is None:
                        name = "_a_%d" % cnt
                        cnt += 1
                    res[name] = formula
            return res

        else:
            return dict(("_a%d" % i, f)
                        for i,f in enumerate(self.get_unsat_core()))

    @clear_pending_pop
    def all_sat(self, important, callback):
        self.push()
        mathsat.msat_all_sat(self.msat_env,
                             [self._var2term(x) for x in important],
                             callback)
        self.pop()

    @clear_pending_pop
    def _push(self, levels=1):
        for _ in xrange(levels):
            mathsat.msat_push_backtrack_point(self.msat_env)

    @clear_pending_pop
    def _pop(self, levels=1):
        for _ in xrange(levels):
            mathsat.msat_pop_backtrack_point(self.msat_env)

    def _var2term(self, var):
        decl = mathsat.msat_find_decl(self.msat_env, var.symbol_name())
        titem = mathsat.msat_make_term(self.msat_env, decl, [])
        return titem

    def set_preferred_var(self, var):
        tvar = self.converter.convert(var)
        mathsat.msat_add_preferred_for_branching(self.msat_env, tvar)
        return

    def print_model(self, name_filter=None):
        if name_filter is not None:
            raise NotImplementedError
        for v in self.converter.symbol_to_decl.keys():
            var = self.mgr.Symbol(v)
            assert var is not None
            print("%s = %s", (v, self.get_value(var)))

    def get_value(self, item):
        self._assert_no_function_type(item)

        titem = self.converter.convert(item)
        tval = mathsat.msat_get_model_value(self.msat_env, titem)

        if mathsat.msat_term_is_number(self.msat_env, tval):
            rep = mathsat.msat_term_repr(tval)
            if self.environment.stc.get_type(item).is_real_type():
                match = re.match(r"(-?\d+)/(\d+)", rep)
                if match is not None:
                    return self.mgr.Real((int(match.group(1)),
                                          int(match.group(2))))
                else:
                    return self.mgr.Real(int(rep))
            elif self.environment.stc.get_type(item).is_int_type():
                return self.mgr.Int(int(rep))
            else:
                assert self.environment.stc.get_type(item).is_bv_type()
                # MathSAT representation is <value>_<width>
                value, width = rep.split("_")
                return self.mgr.BV(int(value), int(width))

        else:
            assert mathsat.msat_term_is_true(self.msat_env, tval) or \
                mathsat.msat_term_is_false(self.msat_env, tval)
            bval = (mathsat.msat_term_is_true(self.msat_env, tval) == 1)
            return self.mgr.Bool(bval)

    def get_model(self):
        assignment = {}
        msat_iterator = mathsat.msat_create_model_iterator(self.msat_env)
        while mathsat.msat_model_iterator_has_next(msat_iterator):
            term, value = mathsat.msat_model_iterator_next(msat_iterator)
            pysmt_term = self.converter.back(term)
            pysmt_value = self.converter.back(value)
            if self.environment.stc.get_type(pysmt_term).is_real_type() and \
               pysmt_value.is_int_constant():
                pysmt_value = self.mgr.Real(pysmt_value.constant_value())
            assignment[pysmt_term] = pysmt_value
        mathsat.msat_destroy_model_iterator(msat_iterator)
        return EagerModel(assignment=assignment, environment=self.environment)

    def _exit(self):
        mathsat.msat_destroy_env(self.msat_env)
        mathsat.msat_destroy_config(self.msat_config)


class MSatConverter(Converter, DagWalker):

    def __init__(self, environment, msat_env):
        DagWalker.__init__(self, environment)

        self.msat_env = msat_env
        self.mgr = environment.formula_manager
        self._get_type = environment.stc.get_type

        # Maps a Symbol into the corresponding msat_decl instance in the msat_env
        self.symbol_to_decl = {}
        # Maps a msat_decl instance inside the msat_env into the corresponding
        # Symbol
        self.decl_to_symbol = {}

        self.boolType = mathsat.msat_get_bool_type(self.msat_env)
        self.realType = mathsat.msat_get_rational_type(self.msat_env)
        self.intType = mathsat.msat_get_integer_type(self.msat_env)

        self.back_memoization = {}

        # Handling of UF bool args
        self._ufrewriter = MSatBoolUFRewriter(environment)

        return

    def back(self, expr):
        return self._walk_back(expr, self.mgr)

    def _most_generic(self, ty1, ty2):
        """Returns teh most generic, yet compatible type between ty1 and ty2"""
        if ty1 == ty2:
            return ty1

        assert ty1 in [types.REAL, types.INT]
        assert ty2 in [types.REAL, types.INT]
        return types.REAL

    def _get_signature(self, term, args):
        """Returns the signature of the given term.
        For example:
        - a term x & y returns a function type Bool -> Bool -> Bool,
        - a term 14 returns Int
        - a term x ? 13 : 15.0 returns Bool -> Real -> Real -> Real
        """
        res = None

        if mathsat.msat_term_is_true(self.msat_env, term) or \
            mathsat.msat_term_is_false(self.msat_env, term) or \
            mathsat.msat_term_is_boolean_constant(self.msat_env, term):
            res = types.BOOL

        elif mathsat.msat_term_is_number(self.msat_env, term):
            ty = mathsat.msat_term_get_type(term)
            if mathsat.msat_is_integer_type(self.msat_env, ty):
                res = types.INT
            elif mathsat.msat_is_rational_type(self.msat_env, ty):
                res = types.REAL
            else:
                assert "_" in str(term), "Unrecognized type for '%s'" % str(term)
                width = int(str(term).split("_")[1])
                res = types.BVType(width)

        elif mathsat.msat_term_is_and(self.msat_env, term) or \
             mathsat.msat_term_is_or(self.msat_env, term) or \
             mathsat.msat_term_is_iff(self.msat_env, term):
            res = types.FunctionType(types.BOOL, [types.BOOL, types.BOOL])

        elif mathsat.msat_term_is_not(self.msat_env, term):
            res = types.FunctionType(types.BOOL, [types.BOOL])

        elif mathsat.msat_term_is_term_ite(self.msat_env, term):
            t1 = self.env.stc.get_type(args[1])
            t2 = self.env.stc.get_type(args[2])
            t = self._most_generic(t1, t2)
            res = types.FunctionType(t, [types.BOOL, t, t])

        elif mathsat.msat_term_is_equal(self.msat_env, term) or \
             mathsat.msat_term_is_leq(self.msat_env, term):
            t1 = self.env.stc.get_type(args[0])
            t2 = self.env.stc.get_type(args[1])
            t = self._most_generic(t1, t2)
            res = types.FunctionType(types.BOOL, [t, t])

        elif mathsat.msat_term_is_plus(self.msat_env, term) or \
             mathsat.msat_term_is_times(self.msat_env, term):
            t1 = self.env.stc.get_type(args[0])
            t2 = self.env.stc.get_type(args[1])
            t = self._most_generic(t1, t2)
            res = types.FunctionType(t, [t, t])

        elif mathsat.msat_term_is_constant(self.msat_env, term):
            ty = mathsat.msat_term_get_type(term)
            if mathsat.msat_is_rational_type(self.msat_env, ty):
                res = types.REAL
            elif mathsat.msat_is_integer_type(self.msat_env, ty):
                res = types.INT
            else:
                _, width = mathsat.msat_is_bv_type(self.msat_env, ty)
                assert width is not None, "Unsupported type for '%s'" % str(term)
                res = types.BVType(width)

        elif mathsat.msat_term_is_uf(self.msat_env, term):
            d = mathsat.msat_term_get_decl(term)
            fun = self.get_symbol_from_declaration(d)
            res = fun.symbol_type()

        elif mathsat.msat_term_is_bv_times(self.msat_env, term) or \
             mathsat.msat_term_is_bv_plus(self.msat_env, term) or \
             mathsat.msat_term_is_bv_minus(self.msat_env, term) or \
             mathsat.msat_term_is_bv_or(self.msat_env, term) or \
             mathsat.msat_term_is_bv_and(self.msat_env, term) or \
             mathsat.msat_term_is_bv_lshl(self.msat_env, term) or \
             mathsat.msat_term_is_bv_lshr(self.msat_env, term) or \
             mathsat.msat_term_is_bv_ashr(self.msat_env, term) or \
             mathsat.msat_term_is_bv_xor(self.msat_env, term) or \
             mathsat.msat_term_is_bv_urem(self.msat_env, term) or \
             mathsat.msat_term_is_bv_udiv(self.msat_env, term) or \
             mathsat.msat_term_is_bv_sdiv(self.msat_env, term) or \
             mathsat.msat_term_is_bv_srem(self.msat_env, term) or \
             mathsat.msat_term_is_bv_concat(self.msat_env, term):
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(t, [t, t])

        elif mathsat.msat_term_is_bv_not(self.msat_env, term) or \
             mathsat.msat_term_is_bv_neg(self.msat_env, term):
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(t, [t])

        elif mathsat.msat_term_is_bv_ult(self.msat_env, term) or \
             mathsat.msat_term_is_bv_slt(self.msat_env, term) or \
             mathsat.msat_term_is_bv_uleq(self.msat_env, term) or \
             mathsat.msat_term_is_bv_sleq(self.msat_env, term):
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(types.BOOL, [t, t])

        elif mathsat.msat_term_is_bv_comp(self.msat_env, term):
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(types.BVType(1), [t, t])

        elif mathsat.msat_term_is_bv_rol(self.msat_env, term)[0] or \
             mathsat.msat_term_is_bv_ror(self.msat_env, term)[0]:
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(t, [t])

        elif mathsat.msat_term_is_bv_sext(self.msat_env, term)[0]:
            _, amount = mathsat.msat_term_is_bv_sext(self.msat_env, term)
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(types.BVType(amount + t.width), [t])

        elif mathsat.msat_term_is_bv_zext(self.msat_env, term)[0]:
            _, amount = mathsat.msat_term_is_bv_zext(self.msat_env, term)
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(types.BVType(amount + t.width), [t])

        elif mathsat.msat_term_is_bv_extract(self.msat_env, term)[0]:
            _, msb, lsb = mathsat.msat_term_is_bv_extract(self.msat_env, term)
            t = self.env.stc.get_type(args[0])
            res = types.FunctionType(types.BVType(msb - lsb + 1), [t])

        else:
            raise TypeError("Unsupported expression:",
                            mathsat.msat_term_repr(term))
        return res

    def _back_single_term(self, term, mgr, args):
        """Builds the pysmt formula given a term and the list of formulae
        obtained by converting the term children.

        :param term: The MathSAT term to be transformed in pysmt formulae
        :type term: MathSAT term

        :param mgr: The formula manager to be sued to build the
        formulae, it should allow for type unsafety.
        :type mgr: Formula manager

        :param args: List of the pysmt formulae obtained by converting
        all the args (obtained by mathsat.msat_term_get_arg()) to
        pysmt formulae
        :type args: List of pysmt formulae

        :returns The pysmt formula representing the given term
        :rtype Pysmt formula
        """
        res = None
        arity = len(args)

        if mathsat.msat_term_is_true(self.msat_env, term):
            res = mgr.TRUE()

        elif mathsat.msat_term_is_false(self.msat_env, term):
            res = mgr.FALSE()

        elif mathsat.msat_term_is_number(self.msat_env, term):
            ty = mathsat.msat_term_get_type(term)
            if mathsat.msat_is_integer_type(self.msat_env, ty):
                res = mgr.Int(int(mathsat.msat_term_repr(term)))
            elif mathsat.msat_is_rational_type(self.msat_env, ty):
                res = mgr.Real(Fraction(mathsat.msat_term_repr(term)))
            else:
                assert "_" in str(term), "Unsupported type for '%s'" % str(term)
                val, width = str(term).split("_")
                val = int(val)
                width = int(width)
                res = mgr.BV(val, width)

        elif mathsat.msat_term_is_and(self.msat_env, term):
            res = mgr.And(args)

        elif mathsat.msat_term_is_or(self.msat_env, term):
            res = mgr.Or(args)

        elif mathsat.msat_term_is_not(self.msat_env, term):
            assert arity == 1
            res = mgr.Not(args[0])

        elif mathsat.msat_term_is_iff(self.msat_env, term):
            assert arity == 2
            res = mgr.Iff(args[0], args[1])

        elif mathsat.msat_term_is_term_ite(self.msat_env, term):
            assert arity == 3
            res = mgr.Ite(args[0], args[1], args[2])

        elif mathsat.msat_term_is_equal(self.msat_env, term):
            assert arity == 2
            res = mgr.Equals(args[0], args[1])

        elif mathsat.msat_term_is_leq(self.msat_env, term):
            assert arity == 2
            res = mgr.LE(args[0], args[1])

        elif mathsat.msat_term_is_plus(self.msat_env, term):
            res = mgr.Plus(args)

        elif mathsat.msat_term_is_times(self.msat_env, term):
            assert arity == 2
            res = mgr.Times(args[0], args[1])

        elif mathsat.msat_term_is_boolean_constant(self.msat_env, term):
            rep = mathsat.msat_term_repr(term)
            res = mgr.Symbol(rep, types.BOOL)

        elif mathsat.msat_term_is_constant(self.msat_env, term):
            rep = mathsat.msat_term_repr(term)

            ty = mathsat.msat_term_get_type(term)
            if mathsat.msat_is_rational_type(self.msat_env, ty):
                res = mgr.Symbol(rep, types.REAL)
            elif mathsat.msat_is_integer_type(self.msat_env, ty):
                res = mgr.Symbol(rep, types.INT)
            else:
                _, width = mathsat.msat_is_bv_type(self.msat_env, ty)
                assert width is not None, "Unsupported variable type for '%s'"%str(term)
                res = mgr.Symbol(rep, types.BVType(width))

        elif mathsat.msat_term_is_uf(self.msat_env, term):
            d = mathsat.msat_term_get_decl(term)
            fun = self.get_symbol_from_declaration(d)
            res = mgr.Function(fun, args)

        elif mathsat.msat_term_is_bv_times(self.msat_env, term):
            assert arity == 2
            res = mgr.BVMul(args[0], args[1])

        elif mathsat.msat_term_is_bv_plus(self.msat_env, term):
            assert arity == 2
            res = mgr.BVAdd(args[0], args[1])

        elif mathsat.msat_term_is_bv_udiv(self.msat_env, term):
            assert arity == 2
            res = mgr.BVUDiv(args[0], args[1])

        elif mathsat.msat_term_is_bv_urem(self.msat_env, term):
            assert arity == 2
            res = mgr.BVURem(args[0], args[1])

        elif mathsat.msat_term_is_bv_extract(self.msat_env, term)[0]:
            assert arity == 1
            res, msb, lsb = mathsat.msat_term_is_bv_extract(self.msat_env, term)
            assert res
            res = mgr.BVExtract(args[0], lsb, msb)

        elif mathsat.msat_term_is_bv_concat(self.msat_env, term):
            assert arity == 2
            res = mgr.BVConcat(args[0], args[1])

        elif mathsat.msat_term_is_bv_or(self.msat_env, term):
            assert arity == 2
            res = mgr.BVOr(args[0], args[1])

        elif mathsat.msat_term_is_bv_xor(self.msat_env, term):
            assert arity == 2
            res = mgr.BVXor(args[0], args[1])

        elif mathsat.msat_term_is_bv_and(self.msat_env, term):
            assert arity == 2
            res = mgr.BVAnd(args[0], args[1])

        elif mathsat.msat_term_is_bv_not(self.msat_env, term):
            assert arity == 1
            res = mgr.BVNot(args[0])

        elif mathsat.msat_term_is_bv_minus(self.msat_env, term):
            assert arity == 2
            res = mgr.BVSub(args[0], args[1])

        elif mathsat.msat_term_is_bv_neg(self.msat_env, term):
            assert arity == 1
            res = mgr.BVSub(args[0])

        elif mathsat.msat_term_is_bv_srem(self.msat_env, term):
            assert arity == 2
            res = mgr.BVSRem(args[0], args[1])

        elif mathsat.msat_term_is_bv_sdiv(self.msat_env, term):
            assert arity == 2
            res = mgr.BVSDiv(args[0], args[1])

        elif mathsat.msat_term_is_bv_ult(self.msat_env, term):
            assert arity == 2
            res = mgr.BVULT(args[0], args[1])

        elif mathsat.msat_term_is_bv_slt(self.msat_env, term):
            assert arity == 2
            res = mgr.BVSLT(args[0], args[1])

        elif mathsat.msat_term_is_bv_uleq(self.msat_env, term):
            assert arity == 2
            res = mgr.BVULE(args[0], args[1])

        elif mathsat.msat_term_is_bv_sleq(self.msat_env, term):
            assert arity == 2
            res = mgr.BVSLE(args[0], args[1])

        elif mathsat.msat_term_is_bv_lshl(self.msat_env, term):
            assert arity == 2
            res = mgr.BVLShl(args[0], args[1])

        elif mathsat.msat_term_is_bv_lshr(self.msat_env, term):
            assert arity == 2
            res = mgr.BVLShr(args[0], args[1])

        elif mathsat.msat_term_is_bv_ashr(self.msat_env, term):
            assert arity == 2
            res = mgr.BVAShr(args[0], args[1])

        elif mathsat.msat_term_is_bv_comp(self.msat_env, term):
            assert arity == 2
            res = mgr.BVComp(args[0], args[1])

        elif mathsat.msat_term_is_bv_zext(self.msat_env, term)[0]:
            assert arity == 2
            res, amount = mathsat.msat_term_is_bv_zext(self.msat_env, term)
            assert res
            res = mgr.BVZExt(args[0], amount)

        elif mathsat.msat_term_is_bv_sext(self.msat_env, term)[0]:
            assert arity == 2
            res, amount = mathsat.msat_term_is_bv_sext(self.msat_env, term)
            assert res
            res = mgr.BVSExt(args[0], amount)

        elif mathsat.msat_term_is_bv_rol(self.msat_env, term)[0]:
            assert arity == 2
            res, amount = mathsat.msat_term_is_bv_ror(self.msat_env, term)
            assert res
            res = mgr.BVRol(args[0], amount)

        elif mathsat.msat_term_is_bv_ror(self.msat_env, term)[0]:
            assert arity == 2
            res, amount = mathsat.msat_term_is_bv_ror(self.msat_env, term)
            assert res
            res = mgr.BVRor(args[0], amount)

        else:
            raise TypeError("Unsupported expression:",
                            mathsat.msat_term_repr(term))
        return res

    def get_symbol_from_declaration(self, decl):
        return self.decl_to_symbol[mathsat.msat_decl_id(decl)]

    def _walk_back(self, term, mgr):
        stack = [term]

        while len(stack) > 0:
            current = stack.pop()
            arity = mathsat.msat_term_arity(current)
            if current not in self.back_memoization:
                self.back_memoization[current] = None
                stack.append(current)
                for i in xrange(arity):
                    son = mathsat.msat_term_get_arg(current, i)
                    stack.append(son)
            elif self.back_memoization[current] is None:
                args=[self.back_memoization[mathsat.msat_term_get_arg(current,i)]
                      for i in xrange(arity)]

                signature = self._get_signature(current, args)
                new_args = []
                for i, a in enumerate(args):
                    t = self.env.stc.get_type(a)
                    if t != signature.param_types[i]:
                        a = mgr.ToReal(a)
                    new_args.append(a)
                res = self._back_single_term(current, mgr, new_args)
                self.back_memoization[current] = res
            else:
                # we already visited the node, nothing else to do
                pass
        return self.back_memoization[term]

    @catch_conversion_error
    def convert(self, formula):
        """Convert a PySMT formula into a MathSat Term.

        This function might throw a InternalSolverError exception if
        an error during conversion occurs.
        """
        # Rewrite to avoid UF with bool args
        rformula = self._ufrewriter.walk(formula)
        res = self.walk(rformula)
        if mathsat.MSAT_ERROR_TERM(res):
            msat_msg = mathsat.msat_last_error_message(self.msat_env)
            raise InternalSolverError(msat_msg)
        if rformula != formula:
            warn("MathSAT convert(): UF with bool arguments have been translated")
        return res

    def walk_and(self, formula, args, **kwargs):
        res = mathsat.msat_make_true(self.msat_env)
        for a in args:
            res = mathsat.msat_make_and(self.msat_env, res, a)
        return res

    def walk_or(self, formula, args, **kwargs):
        res = mathsat.msat_make_false(self.msat_env)
        for a in args:
            res = mathsat.msat_make_or(self.msat_env, res, a)
        return res

    def walk_not(self, formula, args, **kwargs):
        return mathsat.msat_make_not(self.msat_env, args[0])

    def walk_symbol(self, formula, **kwargs):
        if formula not in self.symbol_to_decl:
            self.declare_variable(formula)
        decl = self.symbol_to_decl[formula]
        return mathsat.msat_make_constant(self.msat_env, decl)

    def walk_le(self, formula, args, **kwargs):
        return mathsat.msat_make_leq(self.msat_env, args[0], args[1])

    def walk_lt(self, formula, args, **kwargs):
        leq = mathsat.msat_make_leq(self.msat_env, args[1], args[0])
        return mathsat.msat_make_not(self.msat_env, leq)

    def walk_ite(self, formula, args, **kwargs):
        i = args[0]
        t = args[1]
        e = args[2]

        if self._get_type(formula).is_bool_type():
            impl = self.mgr.Implies(formula.arg(0), formula.arg(1))
            th = self.walk_implies(impl, [i,t])
            nif = self.mgr.Not(formula.arg(1))
            ni = self.walk_not(nif, [i])
            el = self.walk_implies(self.mgr.Implies(nif, formula.arg(2)), [ni,e])
            return mathsat.msat_make_and(self.msat_env, th, el)
        else:
            return mathsat.msat_make_term_ite(self.msat_env, i, t, e)

    def walk_real_constant(self, formula, **kwargs):
        assert type(formula.constant_value()) == Fraction
        frac = formula.constant_value()
        n,d = frac.numerator, frac.denominator
        rep = str(n) + "/" + str(d)
        return mathsat.msat_make_number(self.msat_env, rep)

    def walk_int_constant(self, formula, **kwargs):
        assert type(formula.constant_value()) == int or \
            type(formula.constant_value()) == long
        rep = str(formula.constant_value())
        return mathsat.msat_make_number(self.msat_env, rep)

    def walk_bool_constant(self, formula, **kwargs):
        if formula.constant_value():
            return mathsat.msat_make_true(self.msat_env)
        else:
            return mathsat.msat_make_false(self.msat_env)

    def walk_bv_constant(self, formula, **kwargs):
        rep = str(formula.constant_value())
        width = formula.bv_width()
        return mathsat.msat_make_bv_number(self.msat_env,
                                           rep, width, 10)

    def walk_bv_ult(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_ult(self.msat_env,
                                        args[0], args[1])

    def walk_bv_ule(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_uleq(self.msat_env,
                                         args[0], args[1])

    def walk_bv_slt(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_slt(self.msat_env,
                                        args[0], args[1])

    def walk_bv_sle(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_sleq(self.msat_env,
                                         args[0], args[1])

    def walk_bv_concat(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_concat(self.msat_env,
                                           args[0], args[1])

    def walk_bv_extract(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_extract(self.msat_env,
                                            formula.bv_extract_end(),
                                            formula.bv_extract_start(),
                                            args[0])

    def walk_bv_or(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_or(self.msat_env,
                                       args[0], args[1])

    def walk_bv_not(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_not(self.msat_env, args[0])

    def walk_bv_and(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_and(self.msat_env,
                                        args[0], args[1])

    def walk_bv_xor(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_xor(self.msat_env,
                                        args[0], args[1])

    def walk_bv_add(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_plus(self.msat_env,
                                         args[0], args[1])

    def walk_bv_sub(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_minus(self.msat_env,
                                          args[0], args[1])

    def walk_bv_neg(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_neg(self.msat_env, args[0])

    def walk_bv_mul(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_times(self.msat_env,
                                          args[0], args[1])

    def walk_bv_udiv(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_udiv(self.msat_env,
                                         args[0], args[1])

    def walk_bv_urem(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_urem(self.msat_env,
                                         args[0], args[1])

    def walk_bv_lshl(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_lshl(self.msat_env,
                                         args[0], args[1])

    def walk_bv_lshr(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_lshr(self.msat_env,
                                         args[0], args[1])

    def walk_bv_rol(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_rol(self.msat_env,
                                        formula.bv_rotation_step(),
                                        args[0])

    def walk_bv_ror(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_ror(self.msat_env,
                                        formula.bv_rotation_step(),
                                        args[0])

    def walk_bv_zext(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_zext(self.msat_env,
                                         formula.bv_extend_step(),
                                         args[0])

    def walk_bv_sext(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_sext(self.msat_env,
                                         formula.bv_extend_step(),
                                         args[0])

    def walk_bv_comp(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_comp(self.msat_env,
                                         args[0], args[1])

    def walk_bv_sdiv(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_sdiv(self.msat_env,
                                         args[0], args[1])

    def walk_bv_srem(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_srem(self.msat_env,
                                         args[0], args[1])

    def walk_bv_ashr(self, formula, args, **kwargs):
        return mathsat.msat_make_bv_ashr(self.msat_env,
                                         args[0], args[1])

    def walk_plus(self, formula, args, **kwargs):
        res = mathsat.msat_make_number(self.msat_env, "0")
        for a in args:
            res = mathsat.msat_make_plus(self.msat_env, res, a)
        return res

    def walk_minus(self, formula, args, **kwargs):
        n_one = mathsat.msat_make_number(self.msat_env, "-1")
        n_s2 = mathsat.msat_make_times(self.msat_env, n_one, args[1])
        return mathsat.msat_make_plus(self.msat_env, args[0], n_s2)

    def walk_equals(self, formula, args, **kwargs):
        return mathsat.msat_make_equal(self.msat_env, args[0], args[1])

    def walk_iff(self, formula, args, **kwargs):
        return mathsat.msat_make_iff(self.msat_env, args[0], args[1])

    def walk_implies(self, formula, args, **kwargs):
        neg = self.walk_not(self.mgr.Not(formula.arg(0)), [args[0]])
        return mathsat.msat_make_or(self.msat_env, neg, args[1])

    def walk_times(self, formula, args, **kwargs):
        return mathsat.msat_make_times(self.msat_env, args[0], args[1])

    def walk_function(self, formula, args, **kwargs):
        name = formula.function_name()
        if name not in self.symbol_to_decl:
            self.declare_variable(name)
        decl = self.symbol_to_decl[name]
        return mathsat.msat_make_uf(self.msat_env, decl, args)

    def walk_toreal(self, formula, args, **kwargs):
        # In mathsat toreal is implicit
        return args[0]

    def _type_to_msat(self, tp):
        """Convert a pySMT type into a MathSAT type."""
        if tp.is_bool_type():
            return self.boolType
        elif tp.is_real_type():
            return self.realType
        elif tp.is_int_type():
            return self.intType
        elif tp.is_function_type():
            stps = [self._type_to_msat(x) for x in tp.param_types]
            rtp = self._type_to_msat(tp.return_type)
            msat_type = mathsat.msat_get_function_type(self.msat_env,
                                                       stps,
                                                       rtp)
            if mathsat.MSAT_ERROR_TYPE(msat_type):
                msat_msg = mathsat.msat_last_error_message(self.msat_env)
                raise InternalSolverError(msat_msg)
            return msat_type
        else:
            assert tp.is_bv_type(), "Usupported type for '%s'" % tp
            return mathsat.msat_get_bv_type(self.msat_env, tp.width)

    def declare_variable(self, var):
        if not var.is_symbol(): raise TypeError(var)
        if var.symbol_name() not in self.symbol_to_decl:
            tp = self._type_to_msat(var.symbol_type())
            decl = mathsat.msat_declare_function(self.msat_env,
                                                 var.symbol_name(),
                                                 tp)
            if mathsat.MSAT_ERROR_DECL(decl):
                msat_msg = mathsat.msat_last_error_message(self.msat_env)
                raise InternalSolverError(msat_msg)
            self.symbol_to_decl[var] = decl
            self.decl_to_symbol[mathsat.msat_decl_id(decl)] = var


# Check if we are working on a version MathSAT supporting quantifier elimination
if hasattr(mathsat, "MSAT_EXIST_ELIM_ALLSMT_FM"):
    class MSatQuantifierEliminator(QuantifierEliminator, IdentityDagWalker):

        LOGICS = [LRA]

        def __init__(self, environment, logic=None, algorithm='fm'):
            """Initialize the Quantifier Eliminator using 'fm' or 'lw'.

            fm: Fourier-Motzkin (default)
            lw: Loos-Weisspfenning
            """
            if algorithm not in ['fm', 'lw']:
                raise ValueError("Algorithm can be either 'fm' or 'lw'")
            QuantifierEliminator.__init__(self)
            IdentityDagWalker.__init__(self, env=environment)

            self.set_function(self.walk_identity, op.SYMBOL, op.REAL_CONSTANT,
                              op.BOOL_CONSTANT, op.INT_CONSTANT)
            self.logic = logic
            self.algorithm = algorithm

            self.msat_config = mathsat.msat_create_default_config("QF_LRA")
            self.msat_env = mathsat.msat_create_env(self.msat_config)
            self.converter = MSatConverter(environment, self.msat_env)

        def eliminate_quantifiers(self, formula):
            """Returns a quantifier-free equivalent formula of `formula`."""
            return self.walk(formula)

        def exist_elim(self, variables, formula):
            logic = get_logic(formula, self.env)
            if not logic <= LRA:
                raise NotImplementedError("MathSAT quantifier elimination only"\
                                          " supports LRA (detected logic " \
                                          "is: %s)" % str(logic))

            fterm = self.converter.convert(formula)
            tvars = [self.converter.convert(x) for x in variables]

            algo = mathsat.MSAT_EXIST_ELIM_ALLSMT_FM
            if self.algorithm == 'lw':
                algo = mathsat.MSAT_EXIST_ELIM_VTS

            res = mathsat.msat_exist_elim(self.msat_env, fterm, tvars, algo)

            return self.converter.back(res)

        def walk_forall(self, formula, args, **kwargs):
            assert formula.is_forall()
            variables = formula.quantifier_vars()
            subf = self.env.formula_manager.Not(args[0])
            ex_res = self.exist_elim(variables, subf)
            return self.env.formula_manager.Not(ex_res)

        def walk_exists(self, formula, args, **kwargs):
            # Monolithic quantifier elimination
            assert formula.is_exists()
            variables = formula.quantifier_vars()
            subf = args[0]
            return self.exist_elim(variables, subf)

        def _exit(self):
            mathsat.msat_destroy_env(self.msat_env)
            mathsat.msat_destroy_config(self.msat_config)


    class MSatFMQuantifierEliminator(MSatQuantifierEliminator):
        def __init__(self, environment, logic=None):
            MSatQuantifierEliminator.__init__(self, environment,
                                              logic=logic, algorithm='fm')


    class MSatLWQuantifierEliminator(MSatQuantifierEliminator):
        def __init__(self, environment, logic=None):
            MSatQuantifierEliminator.__init__(self, environment,
                                              logic=logic, algorithm='lw')


class MSatInterpolator(Interpolator):

    LOGICS = [QF_UFLIA, QF_UFLRA, QF_BV]

    def __init__(self, environment, logic=None):
        Interpolator.__init__(self)
        self.msat_env = mathsat.msat_create_env()
        self.converter = MSatConverter(environment, self.msat_env)
        self.environment = environment
        self.logic = logic

    def _exit(self):
        mathsat.msat_destroy_env(self.msat_env)

    def _check_logic(self, formulas):
        for f in formulas:
            logic = get_logic(f, self.environment)
            ok = any(logic <= l for l in self.LOGICS)
            if not ok:
                raise NotImplementedError(
                    "Logic not supported by MathSAT interpolation."
                    "(detected logic is: %s)" % str(logic))


    def binary_interpolant(self, a, b):
        res = self.sequence_interpolant([a, b])
        if res is not None:
            res = res[0]
        return res


    def sequence_interpolant(self, formulas):
        cfg, env = None, None
        try:
            self._check_logic(formulas)

            if len(formulas) < 2:
                raise Exception("interpolation needs at least 2 formulae")

            cfg = mathsat.msat_create_config()
            mathsat.msat_set_option(cfg, "interpolation", "true")
            if self.logic == QF_BV:
                mathsat.msat_set_option(cfg, "theory.bv.eager", "false")
                mathsat.msat_set_option(cfg, "theory.eq_propagaion", "false")
            env = mathsat.msat_create_env(cfg, self.msat_env)

            groups = []
            for f in formulas:
                f = self.converter.convert(f)
                g = mathsat.msat_create_itp_group(env)
                mathsat.msat_set_itp_group(env, g)
                groups.append(g)
                mathsat.msat_assert_formula(env, f)

            res = mathsat.msat_solve(env)
            if res == mathsat.MSAT_UNKNOWN:
                raise Exception("error in mathsat interpolation: %s" %
                                mathsat.msat_last_error_message(env))

            if res == mathsat.MSAT_SAT:
                return None

            pysmt_ret = []
            for i in xrange(1, len(groups)):
                itp = mathsat.msat_get_interpolant(env, groups[:i])
                f = self.converter.back(itp)
                pysmt_ret.append(f)

            return pysmt_ret
        finally:
            if cfg:
                mathsat.msat_destroy_config(cfg)
            if env:
                mathsat.msat_destroy_env(env)


class MSatBoolUFRewriter(IdentityDagWalker):
    """Rewrites an expression containing UF with boolean arguments into an
       equivalent one with only theory UF.

    This is needed because MathSAT does not support UF with boolean
    arguments. This class could implement different rewriting
    strategies. Eventually, we might consider integrating it into the
    Converter directly.
    """

    def __init__(self, environment):
        IdentityDagWalker.__init__(self, environment)
        self.get_type = self.env.stc.get_type
        self.mgr = self.env.formula_manager

    def walk_function(self, formula, args, **kwargs):
        from pysmt.typing import FunctionType
        # Separate arguments
        bool_args = []
        other_args = []
        for a in args:
            if self.get_type(a).is_bool_type():
                bool_args.append(a)
            else:
                other_args.append(a)

        if len(bool_args) == 0:
            # If no Bool Args, return as-is
            return IdentityDagWalker.walk_function(self, formula, args, **kwargs)

        # Build new function type
        rtype = formula.function_name().symbol_type().return_type
        ptype = [self.get_type(a) for a in other_args]
        if len(ptype) == 0:
            ftype = rtype
        else:
            ftype = FunctionType(rtype, ptype)

        # Base-case
        stack = []
        for i in xrange(2**len(bool_args)):
            fname = self.mgr.Symbol("%s#%i" % (formula.function_name(),i), ftype)
            if len(ptype) == 0:
                stack.append(fname)
            else:
                stack.append(self.mgr.Function(fname, tuple(other_args)))

        # Recursive case
        for b in bool_args:
            tmp = []
            while len(stack) > 0:
                lhs = stack.pop()
                rhs = stack.pop()
                # Simplify branches, if b is a constant
                if b.is_true():
                    tmp.append(lhs)
                elif b.is_false():
                    tmp.append(rhs)
                else:
                    ite = self.mgr.Ite(b, lhs, rhs)
                    tmp.append(ite)
            stack = tmp
        res = stack[0]
        return res
