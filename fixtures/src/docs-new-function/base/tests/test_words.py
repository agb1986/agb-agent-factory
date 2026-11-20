from textkit.words import words


def test_words_lowercases_and_drops_punctuation() -> None:
    assert words("Hello, World!") == ["hello", "world"]


def test_words_of_nothing_is_empty() -> None:
    assert words(" ... ") == []
