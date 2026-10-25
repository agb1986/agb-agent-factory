from notes.store import NoteStore


def test_add_returns_the_index() -> None:
    store = NoteStore()
    assert store.add("first") == 0
    assert store.add("second") == 1


def test_all_returns_notes_in_order() -> None:
    store = NoteStore()
    store.add("a")
    store.add("b")
    assert store.all() == ["a", "b"]


def test_new_store_is_empty() -> None:
    assert NoteStore().all() == []


def test_all_returns_a_copy() -> None:
    store = NoteStore()
    store.add("a")
    store.all().append("b")
    assert store.all() == ["a"]
