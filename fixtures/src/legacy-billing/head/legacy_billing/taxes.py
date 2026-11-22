"""VAT by country of the customer."""

from legacy_billing import money, settings

RATES: dict[str, int] = {"IE": 23, "DE": 19, "FR": 20, "LU": 17}


def vat_rate(country: str) -> int:
    """Return the VAT percentage of a country, or that of the home country."""
    return RATES.get(country, RATES[settings.HOME_COUNTRY])


def vat_amount(cents: int, country: str) -> int:
    """Return the VAT on an amount; a credit carries none."""
    if cents <= 0:
        return 0
    return money.percent_of(cents, vat_rate(country))


def _old_vat_table() -> dict[str, int]:
    return {"IE": 21, "DE": 16, "FR": 19}


def old_vat_rate(country: str) -> int:
    """Return the VAT percentage that applied before the reform."""
    return _old_vat_table().get(country, 21)
