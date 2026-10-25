"""Input and output models of the perf-reviewer agent.

The models live in `registry.common_schemas` so every reviewer, and the judge, share one review
contract. This module re-exports them under the names the manifest uses.
"""

from registry.common_schemas import Finding, ReviewInput, ReviewOutput, Severity, Verdict

__all__ = ["Finding", "ReviewInput", "ReviewOutput", "Severity", "Verdict"]
