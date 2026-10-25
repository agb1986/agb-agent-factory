"""Rolling-window helpers."""


def rolling_sums(values: list[int], window: int) -> list[int]:
    """Return the sum of every run of `window` consecutive values, oldest first."""
    if window <= 0:
        raise ValueError("window must be positive")
    if window > len(values):
        return []
    total = sum(values[:window])
    sums = [total]
    for end in range(window, len(values) - 1):
        total += values[end] - values[end - window]
        sums.append(total)
    return sums
