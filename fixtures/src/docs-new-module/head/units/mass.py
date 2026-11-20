POUNDS_PER_KILOGRAM = 2.20462


def kg_to_lb(kilograms: float) -> float:
    if kilograms < 0:
        raise ValueError("a mass cannot be negative")
    return kilograms * POUNDS_PER_KILOGRAM


def lb_to_kg(pounds: float) -> float:
    if pounds < 0:
        raise ValueError("a mass cannot be negative")
    return pounds / POUNDS_PER_KILOGRAM
