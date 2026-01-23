from decimal import Decimal

from website.views import format_currency, parse_decimal, split_numero_comp


def test_parse_decimal_handles_commas_and_dots():
    assert parse_decimal("1.234,56") == Decimal("1234.56")
    assert parse_decimal("1234,56") == Decimal("1234.56")
    assert parse_decimal("1234.56") == Decimal("1234.56")
    assert parse_decimal("foo") is None


def test_format_currency_formats_with_thousands_and_sign():
    assert format_currency(Decimal("1234.5")) == "$ 1.234,50"
    assert format_currency(Decimal("-1000")) == "-$ 1.000,00"


def test_split_numero_comp_normalizes_parts():
    assert split_numero_comp("0002-00000147") == ("2", "147")
    assert split_numero_comp("001200000003") == ("12", "3")
