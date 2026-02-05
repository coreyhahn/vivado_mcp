#!/usr/bin/env python3
"""
Vivado MCP Server - Direct integration with AMD/Xilinx Vivado.

This module implements a Model Context Protocol (MCP) server that provides
AI assistants (like Claude) with direct access to AMD/Xilinx Vivado FPGA
development tools. It enables:

- Session management: Start/stop persistent Vivado TCL sessions
- Project management: Open/close Vivado projects (.xpr files)
- Design flow: Run synthesis, implementation, and bitstream generation
- Reports: Get timing, utilization, and other analysis reports
- Design queries: Explore hierarchy, ports, nets, and cells
- Simulation: Control Vivado's behavioral simulator (xsim)
- Raw TCL: Execute arbitrary TCL commands for advanced operations

Architecture:
    The server maintains a singleton VivadoSession that keeps Vivado running
    in TCL mode. Commands are sent via pexpect and results are parsed and
    returned as structured JSON. This avoids the ~30 second startup time
    for each Vivado command.

MCP Protocol:
    The server uses the MCP stdio transport, communicating via stdin/stdout
    with JSON-RPC messages. Tools are exposed via the @server.list_tools()
    and @server.call_tool() decorators.

Usage:
    # Start the server (typically done by Claude Code or another MCP client)
    python -m vivado_mcp

    # Or via the console script (after pip install)
    vivado-mcp

Example workflow (from an AI assistant):
    1. start_session - Start Vivado
    2. open_project - Open your .xpr file
    3. run_synthesis - Synthesize the design
    4. get_timing_summary - Check timing results
    5. get_utilization - Check resource usage
    6. stop_session - Clean up when done

Author: Created with Claude (Anthropic)
License: MIT
"""

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


# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# Feature requests are stored persistently so users can track requested features
FEATURE_REQUESTS_FILE = Path(__file__).parent / "data" / "feature_requests.json"

# Report management configuration
# Reports are written to temp files when they exceed inline size limits
REPORTS_DIR = Path("/tmp/vivado_mcp")

# Maximum characters to return inline in a response
# Larger reports should use generate_full_report + read_report_section
MAX_RESPONSE_CHARS = 8000  # ~8KB limit for inline responses

# How long to keep cached report files before cleanup (in hours)
REPORT_CACHE_HOURS = 1

# In-memory cache mapping report_id -> metadata (file path, type, etc.)
# This allows quick lookup of previously generated reports
_report_cache: dict[str, dict] = {}


# =============================================================================
# FEATURE REQUEST MANAGEMENT
# =============================================================================

def load_feature_requests() -> list[dict]:
    """
    Load feature requests from the persistent JSON file.

    Feature requests allow the AI assistant to record when it encounters
    limitations or wishes it had a tool that doesn't exist. This helps
    guide future development of the MCP server.

    Returns:
        List of feature request dictionaries, or empty list if file
        doesn't exist or can't be parsed.
    """
    if FEATURE_REQUESTS_FILE.exists():
        try:
            return json.loads(FEATURE_REQUESTS_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_feature_request(request: dict) -> None:
    """
    Save a feature request to the persistent JSON file.

    Args:
        request: Dictionary containing the feature request with fields:
            - id: Auto-assigned sequential ID
            - title: Short description of the feature
            - description: Detailed explanation of what's needed
            - use_case: The specific task that prompted this request
            - priority: low/medium/high
            - timestamp: ISO format timestamp
            - status: "pending" (could be updated to "implemented" later)
    """
    requests = load_feature_requests()
    requests.append(request)
    # Ensure the data directory exists
    FEATURE_REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEATURE_REQUESTS_FILE.write_text(json.dumps(requests, indent=2))


# =============================================================================
# RESPONSE TRUNCATION
# =============================================================================

def truncate_response(content: str, max_chars: int = MAX_RESPONSE_CHARS) -> dict:
    """
    Truncate response content if it exceeds max_chars.

    Large Vivado reports can be tens of thousands of lines. Rather than
    overwhelming the AI context window, we truncate and provide metadata
    about what was cut. The user can then use generate_full_report to
    get the complete output to a file.

    Args:
        content: The full content string to potentially truncate
        max_chars: Maximum characters to return (default: MAX_RESPONSE_CHARS)

    Returns:
        Dictionary with:
            - content: The (possibly truncated) content
            - truncated: Boolean indicating if truncation occurred
            - total_chars: Original content length
            - total_lines: Original line count
            - returned_chars: Characters in truncated content (if truncated)
            - returned_lines: Lines in truncated content (if truncated)
            - truncation_message: Human-readable message about truncation
    """
    total_chars = len(content)
    total_lines = content.count('\n') + 1

    # If content fits, return it unchanged
    if total_chars <= max_chars:
        return {
            "content": content,
            "truncated": False,
            "total_chars": total_chars,
            "total_lines": total_lines
        }

    # Truncate to max_chars, but try to end at a line boundary
    # This makes the output more readable and avoids cutting mid-line
    truncated_content = content[:max_chars]
    last_newline = truncated_content.rfind('\n')

    # Only use the newline boundary if we keep >80% of the allowed content
    # Otherwise we might lose too much useful data
    if last_newline > max_chars * 0.8:
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


def verify_run_status(session, run_name: str) -> dict:
    """
    Verify actual Vivado run status instead of relying on output parsing.

    Vivado run status is stored as properties on the run object. This function
    queries those properties directly, which is more reliable than parsing
    text output that may contain misleading strings.

    Args:
        session: VivadoSession instance
        run_name: Name of the run to check (e.g., "synth_1", "impl_1")

    Returns:
        Dictionary with:
        - run_name: The run that was checked
        - status: Vivado's STATUS property (e.g., "synth_design Complete!")
        - progress: Vivado's PROGRESS property (e.g., "100%")
        - actually_succeeded: True if run completed successfully
        - actually_failed: True if run failed
    """
    status_result = session.run_tcl(f"get_property STATUS [get_runs {run_name}]")
    progress_result = session.run_tcl(f"get_property PROGRESS [get_runs {run_name}]")

    status = status_result.output.strip() if status_result.success else "unknown"
    progress = progress_result.output.strip() if progress_result.success else "unknown"

    # Determine actual success/failure from status string
    # Successful runs have "Complete!" in status
    # Failed runs have "ERROR" in status
    status_lower = status.lower()
    return {
        "run_name": run_name,
        "status": status,
        "progress": progress,
        "actually_succeeded": "complete" in status_lower,
        "actually_failed": "error" in status_lower,
    }


# =============================================================================
# REPORT FILE MANAGEMENT
# =============================================================================

def ensure_reports_dir() -> Path:
    """
    Ensure the reports directory exists and clean up old reports.

    This function is called before generating new reports. It:
    1. Creates the reports directory if it doesn't exist
    2. Removes any report files older than REPORT_CACHE_HOURS
    3. Cleans up the in-memory cache for deleted files

    Returns:
        Path to the reports directory
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Calculate cutoff timestamp for old reports
    cutoff = datetime.now().timestamp() - (REPORT_CACHE_HOURS * 3600)

    # Scan for and remove old report files
    for report_file in REPORTS_DIR.glob("*.txt"):
        try:
            if report_file.stat().st_mtime < cutoff:
                report_file.unlink()
                # Also remove from in-memory cache if present
                report_id = report_file.stem
                _report_cache.pop(report_id, None)
        except OSError:
            pass  # Ignore errors during cleanup

    return REPORTS_DIR


def generate_report_id() -> str:
    """
    Generate a unique 8-character report ID.

    Uses UUID4 for uniqueness, truncated to 8 chars for readability.
    The ID is used to reference reports across tool calls.

    Returns:
        8-character hexadecimal string (e.g., "a1b2c3d4")
    """
    return str(uuid.uuid4())[:8]


def get_hierarchy_depth(path: str) -> int:
    """
    Get the depth of a hierarchical path.

    Vivado uses "/" to separate hierarchy levels (e.g., "cpu/alu/adder").
    This function counts the depth to help filter hierarchy queries.

    Args:
        path: Hierarchical path string

    Returns:
        Depth as integer (0 for top level, 1 for first level children, etc.)
    """
    return path.count('/')


# =============================================================================
# MCP SERVER INSTANCE
# =============================================================================

# Create the MCP server instance
# The name "vivado" is used as the server identifier in MCP communications
server = Server("vivado")


# =============================================================================
# VIVADO OUTPUT PARSERS
# =============================================================================
# These functions parse Vivado's text-based reports into structured data
# that's easier for AI assistants to work with.

def parse_timing_summary(output: str) -> dict:
    """
    Parse a Vivado timing summary report into structured data.

    Timing summary reports contain critical information about whether
    the design meets timing requirements. Key metrics:

    - WNS (Worst Negative Slack): Most critical setup timing margin
      Positive = timing met, Negative = timing violation
    - TNS (Total Negative Slack): Sum of all negative setup slacks
    - WHS (Worst Hold Slack): Most critical hold timing margin
    - THS (Total Hold Slack): Sum of all negative hold slacks
    - WPWS (Worst Pulse Width Slack): For pulse width requirements
    - TPWS (Total Pulse Width Slack): Sum of pulse width violations

    Args:
        output: Raw text output from report_timing_summary

    Returns:
        Dictionary with parsed metrics and "met" boolean indicating
        if all timing is met (WNS >= 0 and WHS >= 0)
    """
    result = {
        "wns": None,   # Worst Negative Slack (setup)
        "tns": None,   # Total Negative Slack (setup)
        "whs": None,   # Worst Hold Slack
        "ths": None,   # Total Hold Slack
        "wpws": None,  # Worst Pulse Width Slack
        "tpws": None,  # Total Pulse Width Slack
        "failing_endpoints": 0,
        "met": False,
        "raw": output  # Keep raw output for detailed analysis
    }

    # Parse WNS/TNS (setup timing) using regex
    # Format: "WNS(ns)      :  1.234" or similar
    wns_match = re.search(r"WNS\(ns\)\s*:\s*([-\d.]+)", output)
    tns_match = re.search(r"TNS\(ns\)\s*:\s*([-\d.]+)", output)
    if wns_match:
        result["wns"] = float(wns_match.group(1))
    if tns_match:
        result["tns"] = float(tns_match.group(1))

    # Parse WHS/THS (hold timing)
    whs_match = re.search(r"WHS\(ns\)\s*:\s*([-\d.]+)", output)
    ths_match = re.search(r"THS\(ns\)\s*:\s*([-\d.]+)", output)
    if whs_match:
        result["whs"] = float(whs_match.group(1))
    if ths_match:
        result["ths"] = float(ths_match.group(1))

    # Parse count of failing endpoints
    fail_match = re.search(r"(\d+)\s+failing\s+endpoint", output, re.IGNORECASE)
    if fail_match:
        result["failing_endpoints"] = int(fail_match.group(1))

    # Determine if timing is met: both setup and hold must have non-negative slack
    if result["wns"] is not None and result["whs"] is not None:
        result["met"] = result["wns"] >= 0 and result["whs"] >= 0

    return result


def parse_utilization(output: str) -> dict:
    """
    Parse a Vivado utilization report into structured data.

    Utilization reports show how much of each FPGA resource type is used.
    This is critical for understanding if a design will fit and for
    optimization decisions.

    Resource types tracked:
    - LUT: Look-Up Tables (combinational logic)
    - FF: Flip-Flops (registers/sequential logic)
    - BRAM: Block RAM (on-chip memory)
    - DSP: DSP slices (multipliers, MACs)
    - IO: Input/Output pins

    Args:
        output: Raw text output from report_utilization

    Returns:
        Dictionary with each resource type containing:
        - used: Number of resources used
        - available: Total resources on the device
        - percent: Utilization percentage
    """
    result = {
        "lut": {"used": 0, "available": 0, "percent": 0},
        "ff": {"used": 0, "available": 0, "percent": 0},
        "bram": {"used": 0, "available": 0, "percent": 0},
        "dsp": {"used": 0, "available": 0, "percent": 0},
        "io": {"used": 0, "available": 0, "percent": 0},
        "raw": output  # Keep raw output for detailed analysis
    }

    # Regex patterns for each resource type
    # Vivado's table format: "Resource | Used | Fixed | Available | Util%"
    # Different device families use slightly different names
    patterns = {
        "lut": r"(?:Slice LUTs|CLB LUTs)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "ff": r"(?:Slice Registers|CLB Registers)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "bram": r"Block RAM Tile\s*\|\s*(\d+\.?\d*)\s*\|\s*\d+\s*\|\s*(\d+\.?\d*)\s*\|\s*([\d.]+)",
        "dsp": r"DSPs?\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)",
        "io": r"(?:Bonded IOB|Bonded User I/O)\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([\d.]+)"
    }

    # Apply each pattern and extract values
    for resource, pattern in patterns.items():
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            result[resource]["used"] = float(match.group(1))
            result[resource]["available"] = float(match.group(2))
            result[resource]["percent"] = float(match.group(3))

    return result


def parse_messages(output: str) -> dict:
    """
    Parse Vivado messages into categorized lists.

    Vivado outputs messages with severity prefixes:
    - ERROR: Design or tool errors that must be fixed
    - CRITICAL WARNING: Serious issues that may cause problems
    - WARNING: Potential issues to review
    - INFO: Informational messages

    Args:
        output: Raw text output containing Vivado messages

    Returns:
        Dictionary with lists of messages by category
    """
    result = {
        "errors": [],
        "critical_warnings": [],
        "warnings": [],
        "info": [],
        "raw": output
    }

    # Categorize each line by its severity prefix
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


def parse_timing_paths_summary(output: str, max_paths: int = 5) -> list[dict]:
    """
    Extract structured summary of timing paths from report_timing output.

    Parses Vivado's timing path reports to extract key information about
    each path without the verbose detailed breakdown.

    Args:
        output: Raw text output from report_timing command
        max_paths: Maximum number of paths to return (default: 5)

    Returns:
        List of dictionaries, each containing:
        - slack: Path slack in ns (negative = failing)
        - source: Source register/port name
        - destination: Destination register/port name
        - source_clock: Source clock domain (if applicable)
        - dest_clock: Destination clock domain (if applicable)
        - requirement: Timing requirement in ns
        - data_path_delay: Data path delay in ns
        - logic_levels: Number of logic levels
    """
    paths = []

    # Split output into individual path blocks
    # Each path starts with "Slack" line
    path_blocks = re.split(r'\n(?=Slack\s*(?:\([A-Z]+\))?\s*:)', output)

    for block in path_blocks:
        if not block.strip() or 'Slack' not in block:
            continue

        path_info = {}

        # Extract slack value
        slack_match = re.search(r'Slack\s*(?:\([A-Z]+\))?\s*:\s*([-\d.]+)\s*ns', block)
        if slack_match:
            path_info['slack'] = float(slack_match.group(1))

        # Extract source (startpoint)
        source_match = re.search(r'Source:\s*(\S+)', block)
        if source_match:
            path_info['source'] = source_match.group(1)

        # Extract destination (endpoint)
        dest_match = re.search(r'Destination:\s*(\S+)', block)
        if dest_match:
            path_info['destination'] = dest_match.group(1)

        # Extract source clock
        src_clk_match = re.search(r'Source Clock:\s*(\S+)', block)
        if src_clk_match:
            path_info['source_clock'] = src_clk_match.group(1)

        # Extract destination clock
        dst_clk_match = re.search(r'Destination Clock:\s*(\S+)', block)
        if dst_clk_match:
            path_info['dest_clock'] = dst_clk_match.group(1)

        # Extract requirement
        req_match = re.search(r'Requirement:\s*([-\d.]+)\s*ns', block)
        if req_match:
            path_info['requirement'] = float(req_match.group(1))

        # Extract data path delay
        data_delay_match = re.search(r'Data Path Delay:\s*([-\d.]+)\s*ns', block)
        if data_delay_match:
            path_info['data_path_delay'] = float(data_delay_match.group(1))

        # Extract logic levels
        levels_match = re.search(r'Logic Levels:\s*(\d+)', block)
        if levels_match:
            path_info['logic_levels'] = int(levels_match.group(1))

        # Only add if we got meaningful data
        if 'slack' in path_info:
            paths.append(path_info)

        if len(paths) >= max_paths:
            break

    return paths


# =============================================================================
# TOOL DEFINITIONS
# =============================================================================
# MCP tools are the interface exposed to AI assistants. Each tool has:
# - name: Unique identifier for the tool
# - description: What the tool does (shown to the AI)
# - inputSchema: JSON Schema defining the parameters

@server.list_tools()
async def list_tools() -> list[Tool]:
    """
    List all available Vivado tools.

    This function is called by MCP clients to discover available tools.
    Tools are organized into categories:

    1. Session Management: start_session, stop_session, session_status
    2. Project Management: open_project, close_project, get_project_info
    3. Design Flow: run_synthesis, run_implementation, generate_bitstream
    4. Reports/Analysis: get_timing_summary, get_timing_paths, get_utilization, etc.
    5. Design Queries: get_design_hierarchy, get_ports, get_nets, get_cells
    6. Raw TCL: run_tcl for advanced operations
    7. Simulation: launch_simulation, run_simulation, get_signal_value, etc.
    8. Feature Requests: request_feature, list_feature_requests
    9. Report Management: generate_full_report, read_report_section

    Returns:
        List of Tool objects with name, description, and inputSchema
    """
    return [
        # =====================================================================
        # SESSION MANAGEMENT TOOLS
        # =====================================================================
        # These tools control the Vivado process lifecycle

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
        Tool(
            name="check_session_health",
            description="Check if Vivado session is responsive and recover if needed. Use this if commands are timing out or behaving unexpectedly.",
            inputSchema={
                "type": "object",
                "properties": {
                    "auto_recover": {
                        "type": "boolean",
                        "description": "Restart session if unhealthy (default: true)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_host_status",
            description="Get status of this Vivado MCP server host including hostname, free memory, and session state. If free memory is below 64GB, use vivado-snoke instead.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),

        # =====================================================================
        # PROJECT MANAGEMENT TOOLS
        # =====================================================================
        # These tools work with Vivado project files (.xpr)

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

        # =====================================================================
        # DESIGN FLOW TOOLS
        # =====================================================================
        # These tools run the major FPGA design flow steps

        Tool(
            name="run_synthesis",
            description="Run synthesis on the current project",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "integer",
                        "description": "Number of parallel jobs (default: 4)"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 1800 = 30 minutes). Increase for large designs."
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
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 3600 = 60 minutes). Increase for large designs."
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

        # =====================================================================
        # REPORTS AND ANALYSIS TOOLS
        # =====================================================================
        # These tools generate and parse Vivado's analysis reports

        Tool(
            name="get_timing_summary",
            description="Get timing summary (WNS, TNS, WHS, THS). Returns parsed metrics only by default. Use generate_full_report for raw output.",
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
                        "description": "Detail level: 'summary' (default, parsed metrics only), 'standard' (+ truncated raw), 'full' (+ complete raw)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_timing_paths",
            description="Get timing paths for failing or critical paths. Returns structured summary (slack, source, dest, clocks) by default. Use generate_full_report for verbose path details.",
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
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "standard", "full"],
                        "description": "Detail level: 'summary' (default, structured only), 'standard' (+ truncated raw), 'full' (+ complete raw)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_utilization",
            description="Get resource utilization (LUT, FF, BRAM, DSP, IO). Returns parsed metrics only by default. Use generate_full_report for hierarchical details.",
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
                        "description": "Detail level: 'summary' (default, parsed only), 'standard' (+ truncated raw), 'full' (+ complete raw)"
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

        # =====================================================================
        # DESIGN QUERY TOOLS
        # =====================================================================
        # These tools explore the elaborated/synthesized design structure

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

        # =====================================================================
        # RAW TCL TOOL
        # =====================================================================
        # Escape hatch for advanced operations not covered by specific tools

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

        # =====================================================================
        # SIMULATION TOOLS
        # =====================================================================
        # These tools control Vivado's integrated simulator (xsim)

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

        # =====================================================================
        # FEATURE REQUEST TOOLS
        # =====================================================================
        # Allow AI assistants to request new features

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

        # =====================================================================
        # REPORT FILE MANAGEMENT TOOLS
        # =====================================================================
        # Handle large reports that exceed inline response limits

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


# =============================================================================
# TOOL IMPLEMENTATION
# =============================================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """
    Handle tool calls from MCP clients.

    This is the main dispatcher that routes tool calls to their implementations.
    Each tool returns a list containing a single TextContent with JSON-formatted
    results.

    Args:
        name: The tool name being called
        arguments: Dictionary of arguments passed to the tool

    Returns:
        List containing one TextContent with JSON response

    Response format:
        All tools return JSON with at minimum:
        - success: Boolean indicating if the operation succeeded
        - Additional fields specific to each tool

        On error:
        - error: Error message string
        - success: False
    """
    # Get the singleton Vivado session
    session = get_session()

    # =========================================================================
    # SESSION MANAGEMENT
    # =========================================================================

    if name == "start_session":
        # Start Vivado TCL session
        # This spawns a persistent Vivado process that stays running
        vivado_path = arguments.get("vivado_path", "vivado")
        session.vivado_path = vivado_path
        result = session.start()
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "stop_session":
        # Stop Vivado session gracefully
        result = session.stop()
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output
        }, indent=2))]

    elif name == "session_status":
        # Get session statistics (commands run, errors, timing, etc.)
        stats = session.get_stats()
        return [TextContent(type="text", text=json.dumps(stats, indent=2))]

    elif name == "check_session_health":
        # Check if session is responsive and optionally recover
        auto_recover = arguments.get("auto_recover", True)

        if not session.is_running:
            if auto_recover:
                result = session.start()
                return [TextContent(type="text", text=json.dumps({
                    "healthy": result.success,
                    "action": "started",
                    "message": "Session was not running, started new session",
                    "elapsed_ms": result.elapsed_ms
                }, indent=2))]
            else:
                return [TextContent(type="text", text=json.dumps({
                    "healthy": False,
                    "action": "none",
                    "message": "Session not running (auto_recover=false)"
                }, indent=2))]

        # Session thinks it's running, check if actually responsive
        is_healthy = session.is_healthy()

        if is_healthy:
            return [TextContent(type="text", text=json.dumps({
                "healthy": True,
                "action": "none",
                "message": "Session is healthy and responsive"
            }, indent=2))]

        # Session is unresponsive
        if auto_recover:
            result = session.ensure_healthy()
            return [TextContent(type="text", text=json.dumps({
                "healthy": result.success,
                "action": "restarted",
                "message": "Session was unresponsive, restarted",
                "elapsed_ms": result.elapsed_ms
            }, indent=2))]
        else:
            return [TextContent(type="text", text=json.dumps({
                "healthy": False,
                "action": "none",
                "message": "Session is unresponsive (auto_recover=false)"
            }, indent=2))]

    elif name == "get_host_status":
        # Get host system status for memory-based server selection
        import socket
        import psutil

        hostname = socket.gethostname()
        mem = psutil.virtual_memory()
        mem_free_gb = mem.available / (1024 ** 3)
        mem_total_gb = mem.total / (1024 ** 3)

        # Build suggestion based on free memory (64GB threshold)
        suggestion = None
        if mem_free_gb < 64:
            suggestion = f"Low memory ({mem_free_gb:.1f}GB free). Use vivado-snoke instead."

        return [TextContent(type="text", text=json.dumps({
            "hostname": hostname,
            "memory_free_gb": round(mem_free_gb, 1),
            "memory_total_gb": round(mem_total_gb, 1),
            "memory_percent_used": mem.percent,
            "vivado_session_active": session.is_running,
            "suggestion": suggestion
        }, indent=2))]

    # =========================================================================
    # SESSION CHECK
    # =========================================================================
    # All remaining commands require an active Vivado session

    if not session.is_running:
        return [TextContent(type="text", text=json.dumps({
            "error": "Vivado session not running. Call start_session first.",
            "success": False
        }, indent=2))]

    # =========================================================================
    # PROJECT MANAGEMENT
    # =========================================================================

    if name == "open_project":
        # Open a Vivado project file (.xpr)
        project_path = arguments.get("project_path", "")
        # Use braces to handle paths with spaces
        result = session.run_tcl(f"open_project {{{project_path}}}")
        if result.success:
            session.current_project = project_path
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "close_project":
        # Close the current project
        result = session.run_tcl("close_project")
        session.current_project = None
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output
        }, indent=2))]

    elif name == "get_project_info":
        # Get various project properties
        commands = [
            "current_project",                                    # Project name
            "get_property PART [current_project]",               # Target FPGA part
            "get_property TARGET_LANGUAGE [current_project]",    # Verilog/VHDL
            "get_property DIRECTORY [current_project]"           # Project directory
        ]
        results = {}
        for cmd in commands:
            r = session.run_tcl(cmd)
            results[cmd] = r.output
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    # =========================================================================
    # DESIGN FLOW
    # =========================================================================

    elif name == "run_synthesis":
        # Run synthesis with optional parallel jobs
        # reset_run clears previous results, launch_runs starts synthesis,
        # wait_on_run blocks until complete
        jobs = arguments.get("jobs", 4)
        timeout = arguments.get("timeout", 1800)  # 30 min default

        result = session.run_tcl(
            f"reset_run synth_1; launch_runs synth_1 -jobs {jobs}; wait_on_run synth_1",
            timeout_override=timeout
        )

        # Verify actual run status (more reliable than output parsing)
        verification = verify_run_status(session, "synth_1")
        actual_success = verification["actually_succeeded"]

        response = {
            "success": actual_success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms,
            "run_status": verification["status"],
            "run_progress": verification["progress"],
        }

        # Note if there was a mismatch between output parsing and actual status
        if not result.success and actual_success:
            response["note"] = "Output contained error-like strings but run completed successfully"

        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    elif name == "run_implementation":
        # Run place and route
        jobs = arguments.get("jobs", 4)
        timeout = arguments.get("timeout", 3600)  # 60 min default

        result = session.run_tcl(
            f"launch_runs impl_1 -jobs {jobs}; wait_on_run impl_1",
            timeout_override=timeout
        )

        # Verify actual run status (more reliable than output parsing)
        verification = verify_run_status(session, "impl_1")
        actual_success = verification["actually_succeeded"]

        response = {
            "success": actual_success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms,
            "run_status": verification["status"],
            "run_progress": verification["progress"],
        }

        # Note if there was a mismatch between output parsing and actual status
        if not result.success and actual_success:
            response["note"] = "Output contained error-like strings but run completed successfully"

        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    elif name == "generate_bitstream":
        # Generate bitstream (programming file)
        result = session.run_tcl("launch_runs impl_1 -to_step write_bitstream; wait_on_run impl_1")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # =========================================================================
    # REPORTS AND ANALYSIS
    # =========================================================================

    elif name == "get_timing_summary":
        # Get timing summary with parsed metrics
        report_type = arguments.get("report_type", "summary")
        detail_level = arguments.get("detail_level", "summary")

        # Run Vivado timing summary report
        result = session.run_tcl("report_timing_summary -no_header -return_string")

        # Parse the raw output into structured data
        parsed = parse_timing_summary(result.output)
        parsed["success"] = result.success
        parsed["elapsed_ms"] = result.elapsed_ms

        # Control output verbosity based on detail_level
        if detail_level == "summary":
            # Only return parsed metrics, no raw output
            parsed.pop("raw", None)
        elif detail_level == "standard":
            # Truncate raw output if too large (half of max to leave room for other data)
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

    elif name == "get_timing_paths":
        # Get detailed timing path information
        # Useful for debugging timing violations
        num_paths = arguments.get("num_paths", 10)
        slack_threshold = arguments.get("slack_threshold", 0)  # 0 = failing paths only
        path_type = arguments.get("path_type", "setup")
        from_pin = arguments.get("from_pin")
        to_pin = arguments.get("to_pin")
        through = arguments.get("through")
        clock = arguments.get("clock")
        detail_level = arguments.get("detail_level", "summary")

        # Build the report_timing command
        delay_type = "max" if path_type == "setup" else "min"
        cmd = f"report_timing -delay_type {delay_type} -max_paths {num_paths} -slack_lesser_than {slack_threshold}"

        # Add optional path filters
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

        # Build response with filter information
        response = {
            "success": result.success,
            "elapsed_ms": result.elapsed_ms,
            "filters_applied": {
                "path_type": path_type,
                "num_paths": num_paths,
                "slack_threshold": slack_threshold
            }
        }

        # Include any filters that were used
        if from_pin:
            response["filters_applied"]["from_pin"] = from_pin
        if to_pin:
            response["filters_applied"]["to_pin"] = to_pin
        if through:
            response["filters_applied"]["through"] = through
        if clock:
            response["filters_applied"]["clock"] = clock

        # Handle output based on detail level
        if result.success:
            # Always parse paths into structured format
            parsed_paths = parse_timing_paths_summary(result.output, max_paths=num_paths)
            response["paths"] = parsed_paths
            response["path_count"] = len(parsed_paths)

            if detail_level == "summary":
                # Only return structured data, no raw output
                pass
            elif detail_level == "standard":
                # Include truncated raw for reference
                truncated = truncate_response(result.output, MAX_RESPONSE_CHARS // 2)
                response["raw"] = truncated["content"]
                if truncated["truncated"]:
                    response["raw_truncated"] = True
                    response["raw_total_chars"] = truncated["total_chars"]
            elif detail_level == "full":
                # Include complete raw output
                truncated = truncate_response(result.output, MAX_RESPONSE_CHARS)
                response["raw"] = truncated["content"]
                if truncated["truncated"]:
                    response["raw_truncated"] = True
                    response["raw_total_chars"] = truncated["total_chars"]
                    response["truncation_message"] = truncated["truncation_message"]
        else:
            response["error"] = result.output

        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    elif name == "get_utilization":
        # Get resource utilization with parsed metrics
        hierarchical = arguments.get("hierarchical", False)
        detail_level = arguments.get("detail_level", "summary")
        module_filter = arguments.get("module_filter")
        threshold_percent = arguments.get("threshold_percent")

        # Build utilization report command
        cmd = "report_utilization -return_string"
        if hierarchical:
            cmd += " -hierarchical"
            if module_filter:
                cmd += f" -hierarchical_pattern {{{module_filter}}}"

        result = session.run_tcl(cmd)

        # Parse into structured data
        parsed = parse_utilization(result.output)
        parsed["success"] = result.success
        parsed["elapsed_ms"] = result.elapsed_ms

        # Apply threshold filter if specified
        if threshold_percent is not None:
            for resource in ["lut", "ff", "bram", "dsp", "io"]:
                if resource in parsed and parsed[resource]["percent"] < threshold_percent:
                    parsed[resource]["below_threshold"] = True

        # Control output verbosity
        if detail_level == "summary":
            parsed.pop("raw", None)
        elif detail_level == "standard":
            if "raw" in parsed and len(parsed["raw"]) > MAX_RESPONSE_CHARS // 2:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS // 2)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
        elif detail_level == "full":
            if "raw" in parsed:
                truncated = truncate_response(parsed["raw"], MAX_RESPONSE_CHARS)
                parsed["raw"] = truncated["content"]
                if truncated["truncated"]:
                    parsed["raw_truncated"] = True
                    parsed["raw_total_chars"] = truncated["total_chars"]
                    parsed["truncation_message"] = truncated["truncation_message"]

        return [TextContent(type="text", text=json.dumps(parsed, indent=2))]

    elif name == "get_clocks":
        # Get clock information from the design
        result = session.run_tcl("report_clocks -return_string")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "clocks": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_messages":
        # Get Vivado messages filtered by severity
        severity = arguments.get("severity", "all")
        result = session.run_tcl("get_msg_config -rules")
        parsed = parse_messages(result.output)

        # Apply severity filter
        if severity != "all":
            filtered = {
                "error": parsed["errors"],
                "critical": parsed["critical_warnings"],
                "warning": parsed["warnings"]
            }.get(severity, [])
            parsed = {severity: filtered, "raw": parsed["raw"]}
        parsed["success"] = result.success
        return [TextContent(type="text", text=json.dumps(parsed, indent=2))]

    # =========================================================================
    # DESIGN QUERIES
    # =========================================================================

    elif name == "get_design_hierarchy":
        # Get the design hierarchy (instances and modules)
        max_depth = arguments.get("max_depth", 3)
        instance_pattern = arguments.get("instance_pattern", "*")

        # Get all hierarchical cells matching the pattern
        cmd = f"get_cells -hierarchical {{{instance_pattern}}}"
        result = session.run_tcl(cmd)

        if result.success and result.output.strip():
            cells = result.output.strip().split()

            # Filter by hierarchy depth (count '/' separators)
            filtered_cells = []
            for cell in cells:
                depth = get_hierarchy_depth(cell)
                if depth <= max_depth:
                    filtered_cells.append(cell)

            # Build a hierarchical structure for easier visualization
            hierarchy = {}
            for cell in sorted(filtered_cells):
                parts = cell.split('/')
                current = hierarchy
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {"_children": {}}
                    current = current[part]["_children"]

            # Get module reference for each cell (limited for performance)
            cell_refs = {}
            sample_cells = filtered_cells[:100]
            for cell in sample_cells:
                ref_result = session.run_tcl(f"get_property REF_NAME [get_cells {{{cell}}}]")
                if ref_result.success and ref_result.output.strip():
                    cell_refs[cell] = ref_result.output.strip()

            response = {
                "success": True,
                "cells": filtered_cells[:500],  # Limit for response size
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
        # Get top-level I/O ports
        result = session.run_tcl("get_ports *")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "ports": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_nets":
        # Search for nets by pattern
        pattern = arguments.get("pattern", "*")
        limit = arguments.get("limit", 100)
        # Use lrange to limit results
        result = session.run_tcl(f"lrange [get_nets {{{pattern}}}] 0 {limit-1}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "nets": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_cells":
        # Search for cells/instances by pattern
        pattern = arguments.get("pattern", "*")
        limit = arguments.get("limit", 100)
        result = session.run_tcl(f"lrange [get_cells {{{pattern}}}] 0 {limit-1}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "cells": result.output.split() if result.success else [],
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # =========================================================================
    # RAW TCL
    # =========================================================================

    elif name == "run_tcl":
        # Execute arbitrary TCL command (escape hatch for advanced users)
        command = arguments.get("command", "")
        result = session.run_tcl(command)
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    # =========================================================================
    # SIMULATION TOOLS
    # =========================================================================

    elif name == "launch_simulation":
        # Launch Vivado's integrated simulator (xsim)
        mode = arguments.get("mode", "behavioral")

        # Map friendly names to Vivado's mode strings
        mode_map = {
            "behavioral": "behav",                    # RTL simulation
            "post_synth_func": "synth -type func",   # Post-synthesis functional
            "post_synth_timing": "synth -type timing", # Post-synthesis with timing
            "post_impl_func": "impl -type func",     # Post-implementation functional
            "post_impl_timing": "impl -type timing"  # Post-implementation with timing
        }
        sim_mode = mode_map.get(mode, "behav")
        result = session.run_tcl(f"launch_simulation -mode {sim_mode}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": result.output if result.output else f"Simulation launched in {mode} mode",
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "run_simulation":
        # Advance simulation time
        time_val = arguments.get("time", "100ns")
        if time_val.lower() == "all":
            # Run until all events processed (testbench completes)
            result = session.run_tcl("run -all")
        else:
            result = session.run_tcl(f"run {time_val}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "restart_simulation":
        # Reset simulation to time 0
        result = session.run_tcl("restart")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "Simulation restarted" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "close_simulation":
        # Close the simulator
        result = session.run_tcl("close_sim")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "Simulation closed" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_time":
        # Get current simulation time
        result = session.run_tcl("current_time")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "time": result.output.strip() if result.success else None,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_signal_value":
        # Get current value of a single signal
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
        # Get values of multiple signals matching a pattern
        pattern = arguments.get("pattern", "/*")
        radix = arguments.get("radix", "hex")

        # First get list of signals matching pattern
        signals_result = session.run_tcl(f"get_objects -filter {{TYPE == signal || TYPE == port}} {{{pattern}}}")
        if signals_result.success and signals_result.output.strip():
            signals = signals_result.output.strip().split()
            values = {}
            # Limit to 50 signals to avoid overwhelming response
            for sig in signals[:50]:
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
        # Add signals to waveform viewer
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
        # Set the top-level testbench module
        top_module = arguments.get("top_module", "")
        fileset = arguments.get("fileset", "sim_1")
        result = session.run_tcl(f"set_property top {top_module} [get_filesets {fileset}]")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": f"Set simulation top to {top_module}" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_objects":
        # List simulation objects (signals, ports, variables) in a scope
        scope = arguments.get("scope", "/")
        obj_filter = arguments.get("filter", "all")

        # Map filter names to Vivado filter expressions
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
        # List child scopes (hierarchy levels) in simulation
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
        # Step simulation by delta cycles
        count = arguments.get("count", 1)
        result = session.run_tcl(f"step {count}")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "add_breakpoint":
        # Add a breakpoint on signal edge or change
        signal = arguments.get("signal", "")
        condition = arguments.get("condition", "change")

        # Map condition names to Vivado flags
        cond_map = {
            "posedge": "-posedge",  # Rising edge
            "negedge": "-negedge",  # Falling edge
            "change": ""           # Any change
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
        # Remove all breakpoints
        result = session.run_tcl("remove_bps -all")
        return [TextContent(type="text", text=json.dumps({
            "success": result.success,
            "message": "All breakpoints removed" if result.success else result.output,
            "elapsed_ms": result.elapsed_ms
        }, indent=2))]

    elif name == "get_simulation_messages":
        # Get simulation log messages
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

    # =========================================================================
    # FEATURE REQUESTS
    # =========================================================================

    elif name == "request_feature":
        # Submit a feature request for future development
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
        # List all submitted feature requests
        requests = load_feature_requests()
        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "total": len(requests),
            "requests": requests
        }, indent=2))]

    # =========================================================================
    # REPORT FILE MANAGEMENT
    # =========================================================================

    elif name == "generate_full_report":
        # Generate a complete report to a file (for large reports)
        report_type = arguments.get("report_type", "timing")
        options = arguments.get("options", {})
        output_file = arguments.get("output_file")

        # Ensure reports directory exists and clean up old files
        ensure_reports_dir()

        # Generate unique report ID and file path
        report_id = generate_report_id()
        if output_file:
            file_path = Path(output_file)
        else:
            file_path = REPORTS_DIR / f"{report_type}_{report_id}.txt"

        # Map report types to Vivado commands
        report_commands = {
            "timing": "report_timing -max_paths 100",
            "timing_summary": "report_timing_summary",
            "utilization": "report_utilization",
            "hierarchy": "report_hierarchy",
            "clocks": "report_clocks",
            "power": "report_power",
            "drc": "report_drc"  # Design Rule Check
        }

        base_cmd = report_commands.get(report_type, f"report_{report_type}")

        # Apply report-specific options
        if report_type == "utilization" and options.get("hierarchical"):
            base_cmd += " -hierarchical"
        if report_type == "timing" and options.get("num_paths"):
            base_cmd = base_cmd.replace("-max_paths 100", f"-max_paths {options['num_paths']}")

        # Write directly to file using Vivado's -file option
        cmd = f"{base_cmd} -file {{{file_path}}}"
        result = session.run_tcl(cmd)

        if result.success:
            try:
                # Get file statistics
                file_stat = file_path.stat()
                line_count = sum(1 for _ in open(file_path))

                # Cache report metadata for later lookup
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
        # Read a portion of a previously generated report
        report_id = arguments.get("report_id")
        file_path = arguments.get("file_path")
        start_line = arguments.get("start_line", 1)
        num_lines = arguments.get("num_lines", 100)
        search_pattern = arguments.get("search_pattern")

        # Resolve file path from report_id if provided
        if report_id:
            if report_id in _report_cache:
                file_path = _report_cache[report_id]["file_path"]
            else:
                # Try to find file in reports directory by ID
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

            # Read all lines from file
            with open(file_path, 'r') as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)

            # Handle search pattern - find and return context around match
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

            # Extract requested line range (1-indexed to 0-indexed)
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

    # =========================================================================
    # UNKNOWN TOOL
    # =========================================================================

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2))]


# =============================================================================
# SERVER ENTRY POINT
# =============================================================================

async def main():
    """
    Run the MCP server.

    This function starts the MCP server using stdio transport (stdin/stdout).
    It's designed to be launched by an MCP client like Claude Code.

    The server runs until the client closes the connection or sends an
    exit signal.
    """
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


# Allow running directly with: python server.py
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
