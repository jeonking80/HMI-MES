"""Pure spreadsheet safety."""

import pytest

from apps.api.reports_http import csv_cell


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_formula_prefixes_are_escaped(prefix):
    assert csv_cell(prefix + "payload") == "'" + prefix + "payload"
    assert csv_cell("한글") == "한글"
