"""Claude Code CLI MCP server.

A local STDIO MCP server that wraps the Claude Code CLI (`claude`)
and exposes its functionality as tools and prompts for orchestrator agents
(Trae IDE). Inspired by the `agy-mcp-server` (Antigravity CLI) but adapted
to the Claude Code CLI flags and authentication model.

See PLAN.md for the full architecture and CONTRATO_TOOLS.md for the
public tool contract.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"