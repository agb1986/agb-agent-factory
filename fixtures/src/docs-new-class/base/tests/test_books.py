from shelf.books import isbn_ok


def test_isbn_ok_accepts_a_valid_isbn() -> None:
    assert isbn_ok("0-306-40615-2")


def test_isbn_ok_accepts_an_x_check_digit() -> None:
    assert isbn_ok("0-8044-2957-X")


def test_isbn_ok_refuses_a_wrong_check_digit() -> None:
    assert not isbn_ok("0-306-40615-3")
