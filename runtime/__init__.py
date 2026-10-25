"""Runtime modules shared by every agent.

`runtime.runner` runs one LLM agent on the Anthropic SDK tool runner, `runtime.hooks` holds the
protocol it calls around every tool, and `runtime.errors` the typed errors a run raises. Every run
writes one row to the trace store in `runtime.trace`, priced by `runtime.pricing`.

Nothing is re-exported here, so importing the trace store does not import the Anthropic SDK.
"""
