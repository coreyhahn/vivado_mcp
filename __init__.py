"""
Vivado MCP Server - Direct integration with AMD/Xilinx Vivado.

This package provides a Model Context Protocol (MCP) server that allows
AI assistants like Claude to directly interact with AMD/Xilinx Vivado
FPGA development tools.

Features:
    - Session Management: Start/stop persistent Vivado TCL sessions
    - Project Management: Open/close Vivado projects (.xpr files)
    - Design Flow: Run synthesis, implementation, and bitstream generation
    - Reports: Get timing summaries, utilization, and design analysis
    - Design Queries: Explore design hierarchy, ports, nets, and cells
    - Simulation: Control Vivado's integrated simulator (xsim)
    - Raw TCL: Execute arbitrary Vivado TCL commands

Installation:
    pip install -e .

    Or add to your Claude Code MCP configuration:
    {
        "mcpServers": {
            "vivado": {
                "command": "python",
                "args": ["-m", "vivado_mcp"]
            }
        }
    }

Usage:
    The server is typically launched by an MCP client (like Claude Code).
    For manual testing:

    python -m vivado_mcp

Example workflow (from an AI assistant):
    1. start_session - Launch Vivado
    2. open_project - Open a .xpr file
    3. run_synthesis - Synthesize the design
    4. get_timing_summary - Check if timing is met
    5. get_utilization - Check resource usage
    6. stop_session - Clean up

Requirements:
    - Python 3.10+
    - mcp>=1.0.0 (Model Context Protocol library)
    - pexpect (for Vivado process management)
    - AMD/Xilinx Vivado installed and in PATH

Author: Created with Claude (Anthropic)
License: MIT
Version: 0.1.0
"""

import asyncio
from .server import main as _async_main

# Package version
__version__ = "0.1.0"


def main():
    """
    Entry point for the vivado-mcp console script.

    This function is called when running:
    - vivado-mcp (after pip install)
    - python -m vivado_mcp

    It starts the async MCP server event loop.
    """
    asyncio.run(_async_main())


# Public API - what gets imported with "from vivado_mcp import *"
__all__ = ["main", "__version__"]
