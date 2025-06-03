import os
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server with configuration
mcp = FastMCP(
    "File manager",  # Name of the MCP server
    instructions="You are a local file manager that can operate on the local file system.",
    host="0.0.0.0",  # Host address (0.0.0.0 allows connections from any IP)
    port=8004,  # Port number for the server
)


@mcp.tool()
async def add(a: int, b: int) -> str:
    """
    Add two number
    """
    return a + b