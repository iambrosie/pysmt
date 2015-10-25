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
"""Defines constants for the commands of the SMT-LIB"""

SET_INFO='set-info'
RESET_ASSERTIONS='reset-assertions'
GET_VALUE='get-value'
SET_OPTION='set-option'
ASSERT='assert'
CHECK_SAT='check-sat'
EXIT='exit'
SET_LOGIC='set-logic'
DECLARE_FUN='declare-fun'
DECLARE_CONST='declare-const'
DEFINE_FUN='define-fun'
PUSH='push'
POP='pop'
# 2.5
RESET='reset'
RESET_ASSERTIONS='reset-assertions'
CHECK_SAT_ASSUMING='check-sat-assuming'
GET_UNSAT_ASSUMPTION='get-unsat-assumption'
ECHO='echo'
DEFINE_FUN_REC='define-fun-rec',
DEFINE_FUNS_REC='define-funs-rec'
GET_MODEL='get-model'

# These commands were introduced in SMT-LIB 2.5
COMMANDS_2_5 = [
    RESET,
    RESET_ASSERTIONS,
    CHECK_SAT_ASSUMING,
    GET_UNSAT_ASSUMPTION,
    ECHO,
    DEFINE_FUN_REC,
    DEFINE_FUNS_REC,
    GET_MODEL,
]

ALL_COMMANDS = [
    SET_INFO,
    RESET_ASSERTIONS,
    GET_VALUE,
    SET_OPTION,
    ASSERT,
    CHECK_SAT,
    EXIT,
    SET_LOGIC,
    DECLARE_FUN,
    DECLARE_CONST,
    DEFINE_FUN,
    PUSH,
    POP,
] + COMMANDS_2_5
