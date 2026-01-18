#!/usr/bin/env python3
"""Vivado MCP Server - Direct integration with AMD/Xilinx Vivado."""

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .vivado_session import get_session, VivadoSession

# Feature requests storage
FEATURE_REQUESTS_FILE = Path(__file__).parent / "data" / "feature_requests.json"

# Report management
REPORTS_DIR = Path("/tmp/vivado_mcp")
MAX_RESPONSE_CHARS = 50000  # ~50KB limit for inline responses
REPORT_CACHE_HOURS = 1  # Clean up reports older than this

# In-memory cache for report metadata
_report_cache: dict[str, dict] = {}


def load_feature_requests() -> list[dict]:
    """Load feature requests from file."""
    if FEATURE_REQUESTS_FILE.exists():
        try:
            return json.loads(FEATURE_REQUESTS_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_feature_request(request: dict) -> None:
    """Save a feature request to the file."""
    requests = load_feature_requests()
    requests.append(request)
    FEATURE_REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEATURE_REQUESTS_FILE.write_text(json.dumps(requests, indent=2))


def truncate_response(content: str, max_chars: int = MAX_RESPONSE_CHARS) -> dict:
    """
    Truncate response content if it exceeds max_chars.

    Returns dict with:
        - content: truncated content (if needed)
        - truncated: bool indicating if truncation occurred
        - total_chars: original content length
        - total_lines: original line count
    """
    total_chars = len(content)
    total_lines = content.count('\n') + 1

    if total_chars <= max_chars:
        return {
            "content": content,
            "truncated": False,
            "total_chars": total_chars,
            "total_lines": total_lines
        }

    # Truncate at a line boundary if possible
    truncated_content = content[:max_chars]
    last_newline = truncated_content.rfind('\n')
    if last_newline > max_chars * 0.8:  # Only use newline if we keep >80% of content
        truncated_content = truncated_content[:last_newline]

    truncated_lines = truncated_content.count('\n') + 1

    return {
        "content": truncated_content,
        "truncated": True,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "returned_chars": len(truncated_content),
        "returned_lines": truncated_lines,
        "truncation_message": f"Output truncated ({total_chars:,} chars -> {len(truncated_content):,} chars). Use generate_full_report for complete output."
    }


def ensure_reports_dir() -> Path:
    """Ensure the reports directory exists and clean up old reports."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up old reports (older than REPORT_CACHE_HOURS)
    cutoff = datetime.now().timestamp() - (REPORT_CACHE_HOURS * 3600)
    for report_file in REPORTS_DIR.glob("*.txt"):
        try:
            if report_file.stat().st_mtime < cutoff:
                report_file.unlink()
                # Also remove from cache if present
                report_id = report_file.stem
                _report_cache.pop(report_id, None)
        except OSError:
            pass

    return REPORTS_DIR


def generate_report_id() -> str:
    """Generate a unique report ID."""
    return str(uuid.uuid4())[:8]


def get_hierarchy_depth(path: str) -> int:
    """Get the depth of a hierarchical path."""
    return path.count('/')

# Create the MCP server
server = Server("vivado")


# ============================================================================
# Helper functions to parse Vivado output
# ============================================================================

def parse_timing_summary(output: str) -> dict:
    """Parse timing summary report into structured data."""
    result = {
        "wns": None,  # Worst Negative Slack
        "tns": None,  # Total Negative Slack
        "whs": None,  # Worst Hold Slack
        "ths": None,  # Total Hold Slack
        "wpws": None, # Worst Pulse Width Slack
        "tpws": None, # Total Pulse Width Slack
        "failing_endpoints": 0,
        "met": False,
        "raw": output
    }

    # Parse WNS/TNS
    wns_match = re.search(r"WNS\(ns\)\s*:\s*([-\d.]+)", output)
    tns_match = re.search(r"TNS\(ns\)\s*:\s*([-\d.]+)", output)
    if wns_match:
        result["wns"] = float(wns_match.group(1))
    if tns_match:
        result["tns"] = float(tns_match.group(1))

    # Parse WHS/THS
    whs_match = re.search(r"WHS\(ns\)\s*:\s*([-\d.]+)", output)
    ths_match = re.search(r"THS\(ns\)\s*:\s*([-\d.]+)", output)
    if whs_match:
        result["whs"] = float(whs_match.group(1))
    if ths_match:
        result["ths"] = float(ths_match.group(1))

    # Parse failing endpoints
    fail_match = re.search(r"(\d+)\s+failing\s+endpoint", output, re.IGNORECASE)
    if fail_match:
        result["failing_endpoints"] = int(fail_match.group(1))

    # Check if timing is met
    if result["wns"] is not None and result["whs"] is not None:
        result["met"] = result["wns"] >= 0 and result["whs"] >= 0

    return result


def parse_utilization(output: str) -> dict:
    """Parse utilization report into structured data."""
    result = {
        "lut": {"used": 0, "available": 0, "percent": 0},
        "ff": {"used": 0, "available": 0, "percent": 0},
        "bram": {"used": 0, "available": 0, "percent": 0},
        "dsp": {"used": 0, "available": 0, "percent": 0},
        "io": {"used": 0, "available": 0, "percent": 0},
        "raw": output
    }

    # Parse different resource types
    patterns = {
        "lut": r"(?:Slice LUTs|CLB LUTs)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "ff": r"(?:Slice Registers|CLB Registers)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "bram": r"Block RAM Tile\s*\|\s*(\d+\.?\d*)\s*\|\s*\d+\s*\|\s*(\d+\.?\d*)\s*\|\s*([\d.]+)",
        "dsp": r"DSPs?\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "io": r"(?:Bonded IOB|Bonded User I/O)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)"
    }

    for resource, pattern in patterns.items():
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            result[resource]["used"] = float(match.group(1))
            result[resource]["available"] = float(match.group(2))
            result[resource]["percent"] = float(match.group(3))

    return result


def parse_messages(output: str) -> dict:
    """Parse Vivado messages into categorized lists."""
    result = {
        "errors": [],
        "critical_warnings": [],
        "warnings": [],
        "info": [],
        "raw": output
    }

    for line in output.split("\n"):
        line = line.strip()
        if re.match(r"ERROR:", line):
            result["errors"].append(line)
        elif re.match(r"CRITICAL WARNING:", line):
            result["critical_warnings"].append(line)
        elif re.match(r"WARNING:", line):
            result["warnings"].append(line)
        elif re.match(r"INFO:", line):
            result["info"].append(line)

    return result


# ============================================================================
# TOOLS
# ============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available Vivado tools."""
    return [
        # Session management
        Tool(
            name="start_session",
            description="Start a persistent Vivado TCL session. Must be called before other commands.",
            inputSchema={
                "type": "object",
                "properties": {
                    "vivado_path": {
                        "type": "string",
                        "description": "Path to Vivado executable (default: 'vivado' from PATH)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="stop_session",
            description="Stop the Vivado TCL session and free resources",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="session_status",
            description="Get status and statistics of the current Vivado session",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),

        # Project management
        Tool(
            name="open_project",
            description="Open a Vivado project (.xpr file)",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Path to .xpr project file"
                    }
                },
                "required": ["project_path"]
            }
        ),
        Tool(
            name="close_project",
            description="Close the current project",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_project_info",
            description="Get information about the currently open project",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),

        # Design flow
        Tool(
            name="run_synthesis",
            description="Run synthesis on the current project",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "integer",
                        "description": "Number of parallel jobs (default: 4)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="run_implementation",
            description="Run implementation (place and route) on the current project",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "integer",
                        "description": "Number of parallel jobs (default: 4)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="generate_bitstream",
            description="Generate bitstream for the implemented design",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),

        # Reports and analysis
        Tool(
            name="get_timing_summary",
            description="Get timing summary (WNS, TNS, WHS, THS) - returns structured data",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "description": "Type: 'summary' (default), 'setup', 'hold', or 'all'"
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "standard", "full"],
                        "description": "Detail level: 'summary' (parsed metrics only), 'standard' (default), 'full' (include raw report)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_timing_paths",
            description="Get detailed timing paths for failing or critical paths",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "integer",
                        "description": "Number of paths to report (default: 10)"
                    },
                    "slack_threshold": {
                        "type": "number",
                        "description": "Only show paths with slack less than this (default: 0 for failing paths)"
                    },
                    "path_type": {
                        "type": "string",
                        "description": "Type: 'setup' (default) or 'hold'"
                    },
                    "from_pin": {
                        "type": "string",
                        "description": "Filter paths starting from this pin/cell pattern (Vivado -from option)"
                    },
                    "to_pin": {
                        "type": "string",
                        "description": "Filter paths ending at this pin/cell pattern (Vivado -to option)"
                    },
                    "through": {
                        "type": "string",
                        "description": "Filter paths going through this pin/cell pattern (Vivado -through option)"
                    },
                    "clock": {
                        "type": "string",
                        "description": "Filter paths by clock domain name"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_utilization",
            description="Get resource utilization report - returns structured data",
            inputSchema={
                "type": "object",
                "properties": {
                    "hierarchical": {
                        "type": "boolean",
                        "description": "Include hierarchical breakdown (default: false)"
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "standard", "full"],
                        "description": "Detail level: 'summary' (parsed only), 'standard' (default, + top consumers), 'full' (+ raw report)"
                    },
                    "module_filter": {
                        "type": "string",
                        "description": "Wildcard pattern to filter modules in hierarchical report"
                    },
                    "threshold_percent": {
                        "type": "number",
                        "description": "Only show resources above this utilization percentage (0-100)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_clocks",
            description="Get clock information and constraints",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_messages",
            description="Get synthesis/implementation messages (errors, warnings)",
            inputSchema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity: 'all' (default), 'error', 'critical', 'warning'"
                    }
                },
                "required": []
            }
        ),

        # Design queries
        Tool(
            name="get_design_hierarchy",
            description="Get the design hierarchy (modules and instances)",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum hierarchy depth to return (default: 3)"
                    },
                    "instance_pattern": {
                        "type": "string",
                        "description": "Wildcard pattern to filter instances (e.g., '*cpu*', 'core/alu/*')"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_ports",
            description="Get top-level ports of the design",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_nets",
            description="Search for nets in the design",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Wildcard pattern to match net names (default: '*')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 100)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_cells",
            description="Search for cells (instances) in the design",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Wildcard pattern to match cell names (default: '*')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 100)"
                    }
                },
                "required": []
            }
        ),

        # Raw TCL
        Tool(
            name="run_tcl",
            description="Execute a raw TCL command in Vivado. Use for advanced operations not covered by other tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "TCL command to execute"
                    }
                },
                "required": ["command"]
            }
        ),

        # Simulation tools
        Tool(
            name="launch_simulation",
            description="Launch behavioral simulation (xsim). Opens the simulator and loads the design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["behavioral", "post_synth_func", "post_synth_timing", "post_impl_func", "post_impl_timing"],
                        "description": "Simulation mode (default: behavioral)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="run_simulation",
            description="Run the simulation for a specified time",
            inputSchema={
                "type": "object",
                "properties": {
                    "time": {
                        "type": "string",
                        "description": "Time to run (e.g., '100ns', '1us', '10ms', 'all')"
                    }
                },
                "required": ["time"]
            }
        ),
        Tool(
            name="restart_simulation",
            description="Restart the simulation from time 0",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="close_simulation",
            description="Close the current simulation",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_simulation_time",
            description="Get the current simulation time",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_signal_value",
            description="Get the current value of a signal in simulation",
            inputSchema={
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "description": "Full hierarchical signal path (e.g., '/tb/dut/clk', '/tb/dut/data_out')"
                    },
                    "radix": {
                        "type": "string",
                        "enum": ["bin", "hex", "dec", "unsigned", "ascii"],
                        "description": "Display radix (default: hex)"
                    }
                },
                "required": ["signal"]
            }
        ),
        Tool(
            name="get_signal_values",
            description="Get current values of multiple signals matching a pattern",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Signal pattern with wildcards (e.g., '/tb/dut/*', '/tb/dut/data*')"
                    },
                    "radix": {
                        "type": "string",
                        "enum": ["bin", "hex", "dec", "unsigned", "ascii"],
                        "description": "Display radix (default: hex)"
                    }
                },
                "required": ["pattern"]
            }
        ),
        Tool(
            name="add_signals_to_wave",
            description="Add signals to the waveform viewer",
            inputSchema={
                "type": "object",
                "properties": {
                    "signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of signal paths to add (e.g., ['/tb/dut/clk', '/tb/dut/rst'])"
                    }
                },
                "required": ["signals"]
            }
        ),
        Tool(
            name="set_simulation_top",
            description="Set the top module for simulation",
            inputSchema={
                "type": "object",
                "properties": {
                    "top_module": {
                        "type": "string",
                        "description": "Name of the testbench module"
                    },
                    "fileset": {
                        "type": "string",
                        "description": "Simulation fileset (default: sim_1)"
                    }
                },
                "required": ["top_module"]
            }
        ),
        Tool(
            name="get_simulation_objects",
            description="List simulation objects (signals, variables) in a scope",
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Hierarchical scope (e.g., '/tb', '/tb/dut'). Default is root."
                    },
                    "filter": {
                        "type": "string",
                        "enum": ["all", "signals", "ports", "internal"],
                        "description": "Filter by object type (default: all)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_scopes",
            description="List available scopes (hierarchy) in the simulation",
            inputSchema={
                "type": "object",
                "properties": {
                    "parent": {
                        "type": "string",
                        "description": "Parent scope to list children of (default: root)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="step_simulation",
            description="Step the simulation by a delta cycle or time step",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="add_breakpoint",
            description="Add a simulation breakpoint on a signal condition",
            inputSchema={
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "description": "Signal to monitor"
                    },
                    "condition": {
                        "type": "string",
                        "enum": ["posedge", "negedge", "change"],
                        "description": "Trigger condition (default: change)"
                    }
                },
                "required": ["signal"]
            }
        ),
        Tool(
            name="remove_breakpoints",
            description="Remove all breakpoints",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_simulation_messages",
            description="Get simulation log messages (errors, warnings, info)",
            inputSchema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["all", "error", "warning", "info"],
                        "description": "Filter by severity (default: all)"
                    }
                },
                "required": []
            }
        ),

        # Feature requests
        Tool(
            name="request_feature",
            description="Request a new feature or capability for the Vivado MCP server. Use this when you encounter a limitation or wish you had a tool that doesn't exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the feature request"
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what you need and why"
                    },
                    "use_case": {
                        "type": "string",
                        "description": "The specific use case or task you were trying to accomplish"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "How important is this feature? (default: medium)"
                    }
                },
                "required": ["title", "description"]
            }
        ),
        Tool(
            name="list_feature_requests",
            description="List all feature requests that have been submitted",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),

        # Report file management
        Tool(
            name="generate_full_report",
            description="Generate a full Vivado report to a file. Use when inline reports are truncated or you need the complete output.",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": ["timing", "timing_summary", "utilization", "hierarchy", "clocks", "power", "drc"],
                        "description": "Type of report to generate"
                    },
                    "options": {
                        "type": "object",
                        "description": "Report-specific options (e.g., {'hierarchical': true} for utilization)"
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Optional custom output path. Default: /tmp/vivado_mcp/<type>_<id>.txt"
                    }
                },
                "required": ["report_type"]
            }
        ),
        Tool(
            name="read_report_section",
            description="Read a section of a previously generated report file",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "Report ID returned by generate_full_report"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Alternative: direct file path to read"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-indexed, default: 1)"
                    },
                    "num_lines": {
                        "type": "integer",
                        "description": "Number of lines to read (default: 100)"
                    },
                    "search_pattern": {
                        "type": "string",
                        "description": "Regex pattern to find a section (returns lines around first match)"
                    }
                },
                "required": []
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    session = get_session()

    # Session management
    if name == "start_session":
        vivado_path = arguments.get("vivado_path", "vivado")
        session.vivado_path = vivado_path
        result = session.start()
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "stop_session":
        result = session.stop()
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output
        }, indent=2))]

    elif name == "session_status":
        stats = session.get_stats()
        return [TextContent(type="text", text=json.dumps(stats, indent=2))]

    # Check session is running for remaining commands
    if not session.is_running:
        return [TextContent(type="text", text=json.dumps({
            "error": "Vivado session not running. Call start_session first.",
            "success": False
        }, indent=2))]

    # Project management
    if name == "open_project":
        project_path = arguments.get("project_path", "")
        result = session.run_tcl(f"open_project {{{project_path}}}")
        if result.success:
            session.current_project = project_path
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "close_project":
        result = session.run_tcl("close_project")
        session.current_project = None
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output
        }, indent=2))]

    elif name == "get_project_info":
        commands = [
            "current_project",
            "get_property PART [current_project]",
            "get_property TARGET_LANGUAGE [current_project]",
            "get_property DIRECTORY [current_project]"
        ]
        results = {}
        for cmd in commands:
            r = session.run_tcl(cmd)
            results[cmd] = r.output
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    # Design flow
    elif name == "run_synthesis":
        jobs = arguments.get("jobs", 4)
        result = session.run_tcl(f"reset_run synth_1; launch_runs synth_1 -jobs {jobs}; wait_on_run synth_1")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "run_implementation":
        jobs = arguments.get("jobs", 4)
        result = session.run_tcl(f"launch_runs impl_1 -jobs {jobs}; wait_on_run impl_1")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "generate_bitstream":
        result = session.run_tcl("launch_runs impl_1 -to_step write_bitstream; wait_on_run impl_1")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # Reports and analysis
    elif name == "get_timing_summary":
        report_type = arguments.get("report_type", "summary")
        detail_level = arguments.get("detail_level", "standard")

        result = session.run_tcl("report_timing_summary -no_header -return_string")
        parsed = parse_timing_summary(result.output)
        parsed["success"] = result.success
        parsed["elapsed_ms"] = result.elapsed_ms

        # Control output based on detail_level
        if detail_level == "summary":
            # Remove raw output, keep only parsed metrics
            parsed.pop("raw", None)
        elif detail_level == "standard":
            # Truncate raw output if too large
            if "raw" in parsed and len(parsed["raw"]) > MAX_RESPONSE_CHARS // 2:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS // 2)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
        # detail_level == "full": keep complete raw output (but still apply safety truncation)
        elif detail_level == "full":
            if "raw" in parsed:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
                    parsed["truncation_message"] = truncated["truncation_message"]

        return [TextContent(type="text", text=json.dumps(parsed, indent=2))]

    elif name == "get_timing_paths":
        num_paths = arguments.get("num_paths", 10)
        slack_threshold = arguments.get("slack_threshold", 0)
        path_type = arguments.get("path_type", "setup")
        from_pin = arguments.get("from_pin")
        to_pin = arguments.get("to_pin")
        through = arguments.get("through")
        clock = arguments.get("clock")

        delay_type = "max" if path_type == "setup" else "min"
        cmd = f"report_timing -delay_type {delay_type} -max_paths {num_paths} -slack_lesser_than {slack_threshold}"

        # Add optional filters
        if from_pin:
            cmd += f" -from {{{from_pin}}}"
        if to_pin:
            cmd += f" -to {{{to_pin}}}"
        if through:
            cmd += f" -through {{{through}}}"
        if clock:
            cmd += f" -filter {{CLOCK == {clock}}}"

        cmd += " -return_string"
        result = session.run_tcl(cmd)

        # Apply truncation for large outputs
        response = {
            "success": result.success,
            "elapsed_ms": result.elapsed_ms,
            "filters_applied": {
                "path_type": path_type,
                "num_paths": num_paths,
                "slack_threshold": slack_threshold
            }
        }

        if from_pin:
            response["filters_applied"]["from_pin"] = from_pin
        if to_pin:
            response["filters_applied"]["to_pin"] = to_pin
        if through:
            response["filters_applied"]["through"] = through
        if clock:
            response["filters_applied"]["clock"] = clock

        if result.success:
            truncated = truncate_response(result.output, MAX_RESPONSE_CHARS)
            response["paths"] = truncated["content"]
            if truncated["truncated"]:
                response["truncated"] = True
                response["total_chars"] = truncated["total_chars"]
                response["truncation_message"] = truncated["truncation_message"]
        else:
            response["paths"] = result.output

        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    elif name == "get_utilization":
        hierarchical = arguments.get("hierarchical", False)
        detail_level = arguments.get("detail_level", "standard")
        module_filter = arguments.get("module_filter")
        threshold_percent = arguments.get("threshold_percent")

        cmd = "report_utilization -return_string"
        if hierarchical:
            cmd += " -hierarchical"
            if module_filter:
                cmd += f" -hierarchical_pattern {{{module_filter}}}"

        result = session.run_tcl(cmd)
        parsed = parse_utilization(result.output)
        parsed["success"] = result.success
        parsed["elapsed_ms"] = result.elapsed_ms

        # Apply threshold filter if specified
        if threshold_percent is not None:
            for resource in ["lut", "ff", "bram", "dsp", "io"]:
                if resource in parsed and parsed[resource]["percent"] < threshold_percent:
                    parsed[resource]["below_threshold"] = True

        # Control output based on detail_level
        if detail_level == "summary":
            # Remove raw output, keep only parsed metrics
            parsed.pop("raw", None)
        elif detail_level == "standard":
            # Truncate raw output if too large
            if "raw" in parsed and len(parsed["raw"]) > MAX_RESPONSE_CHARS // 2:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS // 2)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
        elif detail_level == "full":
            # Keep complete raw output but apply safety truncation
            if "raw" in parsed:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
                    parsed["truncation_message"] = truncated["truncation_message"]

        return [TextContent(type="text", text=json.dumps(parsed, indent=2))]

    elif name == "get_clocks":
        result = session.run_tcl("report_clocks -return_string")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "clocks": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_messages":
        severity = arguments.get("severity", "all")
        # Get messages from Vivado's message log
        result = session.run_tcl("get_msg_config -rules")
        parsed = parse_messages(result.output)
        if severity != "all":
            filtered = {
                "error": parsed["errors"],
                "critical": parsed["critical_warnings"],
                "warning": parsed["warnings"]
            }.get(severity, [])
            parsed = {severity: filtered, "raw": parsed["raw"]}
        parsed["success"] = result.success
        return [TextContent(type="text", text=json.dumps(parsed, indent=2))]

    # Design queries
    elif name == "get_design_hierarchy":
        max_depth = arguments.get("max_depth", 3)
        instance_pattern = arguments.get("instance_pattern", "*")

        # Get hierarchical cells with pattern filter
        cmd = f"get_cells -hierarchical {{{instance_pattern}}}"
        result = session.run_tcl(cmd)

        if result.success and result.output.strip():
            cells = result.output.strip().split()

            # Filter by depth: count '/' separators
            filtered_cells = []
            for cell in cells:
                depth = get_hierarchy_depth(cell)
                if depth <= max_depth:
                    filtered_cells.append(cell)

            # Build hierarchical structure
            hierarchy = {}
            for cell in sorted(filtered_cells):
                parts = cell.split('/')
                current = hierarchy
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {"_children": {}}
                    current = current[part]["_children"]

            # Also get module reference for each cell (limited to avoid large output)
            cell_refs = {}
            sample_cells = filtered_cells[:100]  # Limit for performance
            for cell in sample_cells:
                ref_result = session.run_tcl(f"get_property REF_NAME [get_cells {{{cell}}}]")
                if ref_result.success and ref_result.output.strip():
                    cell_refs[cell] = ref_result.output.strip()

            response = {
                "success": True,
                "cells": filtered_cells[:500],  # Limit response size
                "cell_count": len(filtered_cells),
                "cell_modules": cell_refs,
                "max_depth": max_depth,
                "elapsed_ms": result.elapsed_ms
            }

            if len(filtered_cells) > 500:
                response["truncated"] = True
                response["total_cells"] = len(filtered_cells)
                response["message"] = "Cell list truncated. Use instance_pattern to filter or generate_full_report for complete hierarchy."
        else:
            response = {
                "success": result.success,
                "cells": [],
                "cell_count": 0,
                "error": result.output if not result.success else "No cells found",
                "elapsed_ms": result.elapsed_ms
            }

        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    elif name == "get_ports":
        result = session.run_tcl("get_ports *")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "ports": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_nets":
        pattern = arguments.get("pattern", "*")
        limit = arguments.get("limit", 100)
        result = session.run_tcl(f"lrange [get_nets {{{pattern}}}] 0 {limit-1}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "nets": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_cells":
        pattern = arguments.get("pattern", "*")
        limit = arguments.get("limit", 100)
        result = session.run_tcl(f"lrange [get_cells {{{pattern}}}] 0 {limit-1}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "cells": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # Raw TCL
    elif name == "run_tcl":
        command = arguments.get("command", "")
        result = session.run_tcl(command)
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # Simulation tools
    elif name == "launch_simulation":
        mode = arguments.get("mode", "behavioral")
        mode_map = {
            "behavioral": "behav",
            "post_synth_func": "synth -type func",
            "post_synth_timing": "synth -type timing",
            "post_impl_func": "impl -type func",
            "post_impl_timing": "impl -type timing"
        }
        sim_mode = mode_map.get(mode, "behav")
        result = session.run_tcl(f"launch_simulation -mode {sim_mode}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output if result.output else f"Simulation launched in {mode} mode",
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "run_simulation":
        time_val = arguments.get("time", "100ns")
        if time_val.lower() == "all":
            result = session.run_tcl("run -all")
        else:
            result = session.run_tcl(f"run {time_val}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "restart_simulation":
        result = session.run_tcl("restart")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "Simulation restarted" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "close_simulation":
        result = session.run_tcl("close_sim")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "Simulation closed" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_time":
        result = session.run_tcl("current_time")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "time": result.output.strip() if result.success else None,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_signal_value":
        signal = arguments.get("signal", "")
        radix = arguments.get("radix", "hex")
        result = session.run_tcl(f"get_value -radix {radix} {{{signal}}}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "signal": signal,
            "value": result.output.strip() if result.success else None,
            "radix": radix,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_signal_values":
        pattern = arguments.get("pattern", "/*")
        radix = arguments.get("radix", "hex")
        # Get list of signals matching pattern
        signals_result = session.run_tcl(f"get_objects -filter {{TYPE == signal || TYPE == port}} {{{pattern}}}")
        if signals_result.success and signals_result.output.strip():
            signals = signals_result.output.strip().split()
            values = {}
            for sig in signals[:50]:  # Limit to 50 signals
                val_result = session.run_tcl(f"get_value -radix {radix} {{{sig}}}")
                if val_result.success:
                    values[sig] = val_result.output.strip()
            return [TextContent(type="text", text=json.dumps({
                "success": True,
                "values": values,
                "radix": radix,
                "elapsed_ms": signals_result.elapsed_ms
            }, indent=2))]
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": "No signals found matching pattern",
            "elapsed_ms": signals_result.elapsed_ms
        }, indent=2))]

    elif name == "add_signals_to_wave":
        signals = arguments.get("signals", [])
        if isinstance(signals, str):
            signals = [signals]
        results = []
        for sig in signals:
            result = session.run_tcl(f"add_wave {{{sig}}}")
            results.append({"signal": sig, "success": result.success})
        return [TextContent(type="text", text=json.dumps({
            "success": all(r["success"] for r in results),
            "results": results
        }, indent=2))]

    elif name == "set_simulation_top":
        top_module = arguments.get("top_module", "")
        fileset = arguments.get("fileset", "sim_1")
        result = session.run_tcl(f"set_property top {top_module} [get_filesets {fileset}]")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": f"Set simulation top to {top_module}" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_objects":
        scope = arguments.get("scope", "/")
        obj_filter = arguments.get("filter", "all")

        filter_map = {
            "all": "",
            "signals": "-filter {TYPE == signal}",
            "ports": "-filter {TYPE == port}",
            "internal": "-filter {TYPE == signal && IS_PORT == false}"
        }
        filter_str = filter_map.get(obj_filter, "")
        result = session.run_tcl(f"get_objects {filter_str} {{{scope}/*}}")
        objects = result.output.strip().split() if result.success and result.output.strip() else []
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "scope": scope,
            "objects": objects,
            "count": len(objects),
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_scopes":
        parent = arguments.get("parent", "/")
        result = session.run_tcl(f"get_scopes {{{parent}/*}}")
        scopes = result.output.strip().split() if result.success and result.output.strip() else []
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "parent": parent,
            "scopes": scopes,
            "count": len(scopes),
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "step_simulation":
        count = arguments.get("count", 1)
        result = session.run_tcl(f"step {count}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "add_breakpoint":
        signal = arguments.get("signal", "")
        condition = arguments.get("condition", "change")
        cond_map = {
            "posedge": "-posedge",
            "negedge": "-negedge",
            "change": ""
        }
        cond_str = cond_map.get(condition, "")
        result = session.run_tcl(f"add_bp {cond_str} {{{signal}}}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "signal": signal,
            "condition": condition,
            "message": result.output if result.output else f"Breakpoint added on {signal}",
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "remove_breakpoints":
        result = session.run_tcl("remove_bps -all")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "All breakpoints removed" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_messages":
        severity = arguments.get("severity", "all")
        if severity == "all":
            result = session.run_tcl("get_msg_config -count")
        else:
            result = session.run_tcl(f"get_msg_config -count -severity {{{severity}}}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "messages": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # Feature requests
    elif name == "request_feature":
        title = arguments.get("title", "")
        description = arguments.get("description", "")
        use_case = arguments.get("use_case", "")
        priority = arguments.get("priority", "medium")

        request = {
            "id": len(load_feature_requests()) + 1,
            "title": title,
            "description": description,
            "use_case": use_case,
            "priority": priority,
            "timestamp": datetime.now().isoformat(),
            "status": "pending"
        }
        save_feature_request(request)

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "message": f"Feature request #{request['id']} submitted: {title}",
            "request": request
        }, indent=2))]

    elif name == "list_feature_requests":
        requests = load_feature_requests()
        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "total": len(requests),
            "requests": requests
        }, indent=2))]

    # Report file management tools
    elif name == "generate_full_report":
        report_type = arguments.get("report_type", "timing")
        options = arguments.get("options", {})
        output_file = arguments.get("output_file")

        # Ensure reports directory exists and clean up old files
        ensure_reports_dir()

        # Generate report ID and file path
        report_id = generate_report_id()
        if output_file:
            file_path = Path(output_file)
        else:
            file_path = REPORTS_DIR / f"{report_type}_{report_id}.txt"

        # Build the report command based on type
        report_commands = {
            "timing": "report_timing -max_paths 100",
            "timing_summary": "report_timing_summary",
            "utilization": "report_utilization",
            "hierarchy": "report_hierarchy",
            "clocks": "report_clocks",
            "power": "report_power",
            "drc": "report_drc"
        }

        base_cmd = report_commands.get(report_type, f"report_{report_type}")

        # Add options for specific report types
        if report_type == "utilization" and options.get("hierarchical"):
            base_cmd += " -hierarchical"
        if report_type == "timing" and options.get("num_paths"):
            base_cmd = base_cmd.replace("-max_paths 100", f"-max_paths {options['num_paths']}")

        # Use -file option to write directly to file
        cmd = f"{base_cmd} -file {{{file_path}}}"
        result = session.run_tcl(cmd)

        if result.success:
            # Get file info
            try:
                file_stat = file_path.stat()
                line_count = sum(1 for _ in open(file_path))

                # Store in cache
                _report_cache[report_id] = {
                    "file_path": str(file_path),
                    "report_type": report_type,
                    "created": datetime.now().isoformat(),
                    "size_bytes": file_stat.st_size,
                    "line_count": line_count
                }

                return [TextContent(type="text", text=json.dumps({
                    "success": True,
                    "report_id": report_id,
                    "file_path": str(file_path),
                    "report_type": report_type,
                    "size_bytes": file_stat.st_size,
                    "line_count": line_count,
                    "message": f"Report written to {file_path}. Use read_report_section to read portions.",
                    "elapsed_ms": result.elapsed_ms
                }, indent=2))]
            except (OSError, IOError) as e:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Report generated but could not read file info: {e}",
                    "file_path": str(file_path),
                    "elapsed_ms": result.elapsed_ms
                }, indent=2))]
        else:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": result.output,
                "elapsed_ms": result.elapsed_ms
            }, indent=2))]

    elif name == "read_report_section":
        report_id = arguments.get("report_id")
        file_path = arguments.get("file_path")
        start_line = arguments.get("start_line", 1)
        num_lines = arguments.get("num_lines", 100)
        search_pattern = arguments.get("search_pattern")

        # Resolve file path
        if report_id:
            if report_id in _report_cache:
                file_path = _report_cache[report_id]["file_path"]
            else:
                # Try to find file in reports directory
                possible_files = list(REPORTS_DIR.glob(f"*_{report_id}.txt"))
                if possible_files:
                    file_path = str(possible_files[0])
                else:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"Report ID '{report_id}' not found in cache or reports directory"
                    }, indent=2))]

        if not file_path:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": "Either report_id or file_path must be provided"
            }, indent=2))]

        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"File not found: {file_path}"
                }, indent=2))]

            with open(file_path, 'r') as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)

            # Handle search pattern
            if search_pattern:
                pattern = re.compile(search_pattern, re.IGNORECASE)
                for i, line in enumerate(all_lines):
                    if pattern.search(line):
                        # Found match, return context around it
                        context_before = num_lines // 4
                        context_after = num_lines - context_before
                        start_line = max(1, i + 1 - context_before)
                        break
                else:
                    return [TextContent(type="text", text=json.dumps({
                        "success": True,
                        "warning": f"Pattern '{search_pattern}' not found in file",
                        "total_lines": total_lines,
                        "file_path": str(file_path)
                    }, indent=2))]

            # Extract requested lines (1-indexed)
            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, start_idx + num_lines)
            selected_lines = all_lines[start_idx:end_idx]

            content = ''.join(selected_lines)

            return [TextContent(type="text", text=json.dumps({
                "success": True,
                "file_path": str(file_path),
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "total_lines": total_lines,
                "returned_lines": len(selected_lines),
                "content": content
            }, indent=2))]

        except (OSError, IOError) as e:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": f"Error reading file: {e}"
            }, indent=2))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2))]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
