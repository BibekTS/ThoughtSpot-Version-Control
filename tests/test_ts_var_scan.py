"""ts_var references are written WITHOUT quotes in ThoughtSpot (docs: ts_var(country_var))."""
import pytest

from scripts.lib.tml_utils import scan_ts_var_references as scan


def _tml(*formulas):
    return {"table": {"rls_rules": {"rules": [{"expr": f} for f in formulas]}}}


@pytest.mark.parametrize("formula,expected", [
    ("country = ts_var(country_var) AND Department = ts_var(department_var)",
     {"country_var", "department_var"}),                                   # documented RLS form
    ("'data developers' in ts_groups OR Department = ts_var(department_var)", {"department_var"}),
    ("[Region] = ts_var('region')", {"region"}),                           # single quotes
    ('[Region] = ts_var("region")', {"region"}),                           # double quotes
    ("[Region] = ts_var ( region )", {"region"}),                          # spaces
    ("TS_VAR(x)", set()),                                                  # case-sensitive
    ("my_ts_var(x)", set()),                                               # not a ts_var call
    ("ts_var()", set()),
    ("ts_username = [User]", set()),
])
def test_forms(formula, expected):
    assert scan(_tml(formula)) == expected


def test_finds_references_anywhere_in_the_tml_and_dedupes():
    tml = {"model": {"formulas": [{"expr": "ts_var(a_var) + ts_var(b_var)"}],
                     "columns": [{"props": {"x": "ts_var(a_var)"}}]}}
    assert scan(tml) == {"a_var", "b_var"}


def test_documented_timezone_formula_with_a_built_in_variable():
    f = ('sql_date_time_op ("CONVERT_TIMEZONE (\'UTC\', {0}, {1})", ts_var(ts_user_timezone), '
         '[Order Date])')
    assert scan(_tml(f)) == set()                  # built-in: never created in the target
    assert scan(_tml(f + " + ts_var(mine)")) == {"mine"}


def test_no_variables():
    assert scan({}) == set() and scan({"a": [1, None, {"b": 2}]}) == set()
