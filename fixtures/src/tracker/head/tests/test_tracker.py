import pytest
from tracker import report, search, storage


@pytest.fixture(autouse=True)
def _empty() -> None:
    storage.clear()


def test_added_issues_are_numbered() -> None:
    assert [storage.add("a").number, storage.add("b").number] == [1, 2]


def test_search_by_label_and_word() -> None:
    storage.add("Crash on start", ["bug"])
    storage.add("Add dark mode", ["feature"])
    assert [i.title for i in search.by_label("bug")] == ["Crash on start"]
    assert [i.title for i in search.by_word("DARK")] == ["Add dark mode"]


def test_summary_counts_states() -> None:
    storage.add("a")
    storage.add("b").state = "closed"
    assert report.summary() == "closed: 1, open: 1"
