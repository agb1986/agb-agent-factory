"""A live read of Sentry through the two read-only tools.

Needs `FACTORY_LIVE=1` and `SENTRY_AUTH_TOKEN`; `SENTRY_ORG` and `SENTRY_PROJECT` name the project
to read. It skips with a message when the organisation or project is unset, and when the project has
no events in the last 30 days there is no issue to read, so it skips for that too.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools import sentry


@pytest.mark.live_sentry
def test_list_events_and_read_one_issue(tmp_path: Path) -> None:
    if not (os.environ.get("SENTRY_ORG") and os.environ.get("SENTRY_PROJECT")):
        pytest.skip("SENTRY_ORG and SENTRY_PROJECT are not set")
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    listing = json.loads(sentry.list_events(tmp_path, since))
    issue_ids = [e["issue_id"] for e in listing["events"] if e["issue_id"]]
    if not issue_ids:
        pytest.skip("the project has no events in the last 30 days")
    issue = json.loads(sentry.read_issue(tmp_path, issue_ids[0]))
    assert issue["id"] == issue_ids[0]
    assert {"count", "release", "exceptions", "breadcrumbs"} <= set(issue)
