"""Operator-facing research helpers.

Modules, and what each one owns:

    web_search  Exa web search with highlights, for market context

Research tools are separate from the engine on purpose: they read the outside
world, they never place an order, and nothing they return is imported by a
strategy or by the ML gate.
"""