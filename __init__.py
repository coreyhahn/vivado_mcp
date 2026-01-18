"""Vivado MCP Server - Direct integration with AMD/Xilinx Vivado."""

import asyncio
from .server import main as _async_main

__version__ = "0.1.0"


def main():
    """Entry point for the vivado-mcp console script."""
    asyncio.run(_async_main())


__all__ = ["main"]
