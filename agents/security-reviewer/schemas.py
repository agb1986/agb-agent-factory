"""Input and output models of the security-reviewer agent.

The security reviewer uses the same review contract as the code reviewer. The models live in
`registry.common_schemas`; this module re-exports them under the names the manifest uses.
"""

from registry.common_schemas import Finding, ReviewInput, ReviewOutput, Severity, Verdict

__all__ = ["Finding", "ReviewInput", "ReviewOutput", "Severity", "Verdict"]
