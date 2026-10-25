# agb-agent-factory

A registry of small, versioned, scoped agents for software engineering work, and the pipelines that run them. Claude does the work (review, judge, write tests, fix). TypeSafe Jev, once proven, decides cheaply whether and where work goes (triage, routing, confidence gates). Gates and routing live in code, never in a model.

This repository is the factory itself. The `agb` CLI in [agb-tools](https://github.com/agb1986/agb-tools) may later grow a thin `agb factory` wrapper over it; the two stay separate because the factory carries heavier dependencies, long-running processes, API keys and a sandbox.

## Status

Planning. Nothing runs yet. The work is tracked as six phase milestones in the [issue tracker](https://github.com/agb1986/agb-agent-factory/issues); see [docs/PLAN.md](docs/PLAN.md) for the plan and [docs/HANDOVER-v2.md](docs/HANDOVER-v2.md) for the original handover that the plan was derived from.

## Design in one paragraph

Every agent is a directory with a manifest (`agent.yaml`), a system prompt and Pydantic input and output schemas, resolved by name and semver. An LLM agent runs on the Anthropic SDK Tool Runner with a scoped allow-list of read-only tools and returns a validated structured output. A Jev agent returns calibrated probabilities over fixed answers and nothing else. Pipelines compose agents, apply gates in code, and record every run, with cost, in a DuckDB trace. Agents are stateless; any prompt, schema or question change bumps the version.

## Stack

Python 3.14 managed by uv. `anthropic` (Tool Runner, structured outputs), `pydantic` v2, `pyyaml`, `packaging`, `duckdb`. Jev access is isolated in one module so the transport (HTTP, OpenRouter, or an SDK when one exists for Python) can change without touching anything else.
