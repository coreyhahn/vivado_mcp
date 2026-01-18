"""
Entry point for running the Vivado MCP server as a module.

This allows running the server with:
    python -m vivado_mcp

Which is equivalent to:
    vivado-mcp  (after pip install)

The server communicates via stdin/stdout using the MCP protocol,
so it's typically launched by an MCP client like Claude Code rather
than run directly from the command line.

For testing, you can run it directly, but you'll need to send
properly formatted JSON-RPC messages to stdin.
"""

import asyncio
from .server import main

# Run the async main function when this module is executed directly
if __name__ == "__main__":
    asyncio.run(main())
