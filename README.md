# Vivado MCP Server

A Model Context Protocol (MCP) server that enables AI assistants like Claude to directly interact with AMD/Xilinx Vivado FPGA development tools.

## Features

- **Session Management**: Start/stop persistent Vivado TCL sessions (avoids 30s startup per command)
- **Project Management**: Open/close Vivado projects (.xpr files)
- **Design Flow**: Run synthesis, implementation, and bitstream generation
- **Reports & Analysis**: Get timing summaries, utilization reports, and design analysis
- **Design Queries**: Explore hierarchy, ports, nets, and cells
- **Simulation**: Control Vivado's integrated simulator (xsim)
- **Raw TCL**: Execute arbitrary Vivado TCL commands for advanced operations

## Requirements

- Python 3.10+
- AMD/Xilinx Vivado installed (tested with 2023.2+)
- Vivado must be in your PATH, or specify the full path when starting a session

## Installation

### From GitHub

```bash
git clone https://github.com/coreyhahn/vivado_mcp.git
cd vivado_mcp
pip install -e .
```

### Configure Claude Code

Add to your Claude Code MCP configuration (`~/.claude/claude_desktop_config.json` or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "vivado": {
      "command": "vivado-mcp"
    }
  }
}
```

Or if you want to specify the Python interpreter:

```json
{
  "mcpServers": {
    "vivado": {
      "command": "python",
      "args": ["-m", "vivado_mcp"]
    }
  }
}
```

## Usage

Once configured, Claude can interact with Vivado through natural language. Example workflow:

1. **Start Vivado session**: "Start a Vivado session"
2. **Open project**: "Open my project at /path/to/project.xpr"
3. **Run synthesis**: "Synthesize the design"
4. **Check timing**: "What's the timing summary? Is timing met?"
5. **Check utilization**: "Show me the resource utilization"
6. **Close session**: "Stop the Vivado session"

## Available Tools

### Session Management
- `start_session` - Start a persistent Vivado TCL session
- `stop_session` - Stop the Vivado session
- `session_status` - Get session statistics

### Project Management
- `open_project` - Open a Vivado project (.xpr)
- `close_project` - Close the current project
- `get_project_info` - Get project information (part, directory, etc.)

### Design Flow
- `run_synthesis` - Run synthesis
- `run_implementation` - Run place and route
- `generate_bitstream` - Generate bitstream

### Reports & Analysis
- `get_timing_summary` - Get timing summary (WNS, TNS, WHS, THS)
- `get_timing_paths` - Get detailed timing paths for failing/critical paths
- `get_utilization` - Get resource utilization (LUTs, FFs, BRAMs, DSPs)
- `get_clocks` - Get clock information
- `get_messages` - Get synthesis/implementation messages

### Design Queries
- `get_design_hierarchy` - Get module/instance hierarchy
- `get_ports` - Get top-level ports
- `get_nets` - Search for nets
- `get_cells` - Search for cells/instances

### Simulation
- `launch_simulation` - Launch behavioral/post-synth/post-impl simulation
- `run_simulation` - Run simulation for specified time
- `restart_simulation` - Restart from time 0
- `close_simulation` - Close the simulator
- `get_simulation_time` - Get current simulation time
- `get_signal_value` - Get a signal's current value
- `get_signal_values` - Get multiple signal values by pattern
- `add_signals_to_wave` - Add signals to waveform viewer
- `set_simulation_top` - Set the testbench module
- `get_simulation_objects` - List signals in a scope
- `get_scopes` - List hierarchy scopes
- `step_simulation` - Step simulation
- `add_breakpoint` - Add signal breakpoint
- `remove_breakpoints` - Remove all breakpoints

### Advanced
- `run_tcl` - Execute raw TCL commands
- `generate_full_report` - Generate full reports to file
- `read_report_section` - Read portions of large reports
- `request_feature` - Request new features
- `list_feature_requests` - List submitted requests

## Architecture

```
┌─────────────────┐     MCP Protocol      ┌─────────────────┐
│   Claude Code   │◄────(JSON-RPC)────────►│  Vivado MCP     │
│   (AI Client)   │     over stdio        │    Server       │
└─────────────────┘                       └────────┬────────┘
                                                   │
                                                   │ pexpect
                                                   │ (TCL commands)
                                                   ▼
                                          ┌─────────────────┐
                                          │ Vivado Process  │
                                          │  (TCL mode)     │
                                          └─────────────────┘
```

The server maintains a persistent Vivado process in TCL mode. Commands are sent via pexpect and output is captured by waiting for the Vivado prompt. This avoids the ~30 second startup overhead that would occur if Vivado were launched for each command.

## Recreating This MCP Server with Claude

This MCP server was created entirely through conversation with Claude. Here's how you can create similar MCP servers:

### 1. Start with a Clear Goal

Tell Claude what you want to build:
> "I want to create an MCP server that lets you control Vivado FPGA tools. You should be able to start Vivado, open projects, run synthesis, check timing, etc."

### 2. Describe the Architecture

Explain the key technical challenges:
> "Vivado takes 30 seconds to start, so we need a persistent session. Vivado has a TCL interface we can use. We need to parse Vivado's text output into structured data."

### 3. Iterate on Tools

Start with basic tools and add more:
1. Session management (start/stop)
2. Project management
3. Design flow commands
4. Reports and queries
5. Simulation control

### 4. Key Design Patterns Used

**Singleton Session**: Only one Vivado process runs at a time
```python
_session: Optional[VivadoSession] = None

def get_session() -> VivadoSession:
    global _session
    if _session is None:
        _session = VivadoSession()
    return _session
```

**pexpect for Process Management**: Keeps Vivado alive between commands
```python
self.child = pexpect.spawn(
    f'{self.vivado_path} -mode tcl -nojournal -nolog',
    encoding='utf-8',
    timeout=self.timeout
)
self.child.expect('Vivado%', timeout=10)  # Wait for prompt
```

**Output Parsing**: Convert text reports to structured JSON
```python
def parse_timing_summary(output: str) -> dict:
    wns_match = re.search(r"WNS\(ns\)\s*:\s*([-\d.]+)", output)
    if wns_match:
        result["wns"] = float(wns_match.group(1))
```

**Response Truncation**: Handle large outputs gracefully
```python
def truncate_response(content: str, max_chars: int) -> dict:
    if len(content) > max_chars:
        return {"content": content[:max_chars], "truncated": True}
```

### 5. MCP Server Structure

Every MCP server needs:

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("your-server-name")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name="...", description="...", inputSchema={...})]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Handle tool calls
    return [TextContent(type="text", text=json.dumps(result))]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                        server.create_initialization_options())
```

### 6. Prompt for Creating Your Own MCP Server

Use this prompt template with Claude:

```
I want to create an MCP server for [YOUR TOOL].

Background:
- [Tool] is a [description] that [what it does]
- It has a [CLI/API/etc] interface that accepts [commands/requests]
- Key operations I want to support: [list operations]

Technical considerations:
- [Startup time, persistent state, output formats, etc.]

Please help me create an MCP server with:
1. Session/connection management
2. Core operations as tools
3. Proper error handling
4. Structured JSON responses
5. Comprehensive code comments

Start with the basic structure and we'll iterate from there.
```

## Contributing

Contributions welcome! Please feel free to submit issues and pull requests.

## License

MIT License - see LICENSE file for details.

## Acknowledgments

- Created with [Claude](https://claude.ai) (Anthropic)
- Uses the [Model Context Protocol](https://modelcontextprotocol.io) specification
- Integrates with [AMD/Xilinx Vivado](https://www.xilinx.com/products/design-tools/vivado.html)
