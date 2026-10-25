from dedupe.ids import normalise_ids, unique_ids


def test_normalise_ids_strips_lowercases_and_drops_blanks() -> None:
    assert normalise_ids([" A1 ", "", "b2", "   "]) == ["a1", "b2"]


def test_unique_ids_keeps_the_first_occurrence() -> None:
    assert unique_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
