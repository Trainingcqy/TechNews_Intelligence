"""MCP adapter package (in-process baseline implementation)."""

from .server import InProcessMCPServer, build_newsdb_server

__all__ = ["InProcessMCPServer", "build_newsdb_server"]
