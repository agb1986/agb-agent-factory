# /// script
# requires-python = ">=3.14"
# dependencies = ["typesafe-sdk==0.7.5"]
# ///
"""Ask Jev one Noul question and print p. Export TYPESAFE_API_KEY, then `uv run` this file."""

from typesafe_sdk import Noul, TypeSafeClient  # ty: ignore[unresolved-import]

with TypeSafeClient(model="jev-1.14.0") as client:  # reads TYPESAFE_API_KEY
    response = client.system_one(
        state="Fixes a typo in README.md.",
        questions={"needs_review": Noul(instructions="Does this change need code review?")},
    )
print(f"model={response.model} p={response.nouls['needs_review'].noul:.3f}")
