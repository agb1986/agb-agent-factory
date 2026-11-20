import pytest
from textkit.words import slugify, words


def test_words_lowercases_and_drops_punctuation() -> None:
    assert words("Hello, World!") == ["hello", "world"]


def test_words_of_nothing_is_empty() -> None:
    assert words(" ... ") == []


def test_slugify_joins_the_words() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("Hello, World!", sep="_") == "hello_world"


def test_slugify_refuses_text_with_no_words() -> None:
    with pytest.raises(ValueError, match="no words"):
        slugify("!!!")
