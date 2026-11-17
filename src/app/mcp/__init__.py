"""MCP servers as backends for the agents' typed tools (metrics, logs, deploy history).

MCP is a transport here, not a way for agents to discover tools: an agent still calls `metrics.query(service,
metric, window)`, and this package decides, from a reviewed manifest, which MCP server and tool answer it and
how the answer is turned back into our own types. See docs/ARCHITECTURE_V2.md, workstream A.
"""
