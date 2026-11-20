import pytest
from shelf.books import Shelf, isbn_ok


def test_isbn_ok_accepts_a_valid_isbn() -> None:
    assert isbn_ok("0-306-40615-2")


def test_isbn_ok_accepts_an_x_check_digit() -> None:
    assert isbn_ok("0-8044-2957-X")


def test_isbn_ok_refuses_a_wrong_check_digit() -> None:
    assert not isbn_ok("0-306-40615-3")


def test_shelf_finds_a_book_with_or_without_hyphens() -> None:
    shelf = Shelf()
    shelf.add("0-306-40615-2", "Signals")
    assert shelf.find("0306406152") == "Signals"
    assert shelf.find("0-8044-2957-X") is None


def test_shelf_refuses_a_bad_isbn() -> None:
    with pytest.raises(ValueError, match="bad ISBN"):
        Shelf().add("123", "Nothing")
