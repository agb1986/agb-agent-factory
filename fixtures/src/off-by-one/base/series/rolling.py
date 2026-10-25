"""Rolling-window helpers."""


def rolling_sums(values: list[int], window: int) -> list[int]:
    """Return the sum of every run of `window` consecutive values, oldest first."""
    if window <= 0:
        raise ValueError("window must be positive")
    sums = []
    for start in range(len(values) - window + 1):
        sums.append(sum(values[start : start + window]))
    return sums
