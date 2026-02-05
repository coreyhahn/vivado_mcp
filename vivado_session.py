"""
Vivado TCL Session Manager - Maintains a persistent Vivado process using pexpect.

This module provides the core Vivado interaction layer for the MCP server.
It manages a persistent Vivado TCL session, avoiding the ~30 second startup
overhead that would occur if Vivado were launched for each command.

Architecture:
    The VivadoSession class spawns Vivado in TCL mode (-mode tcl) using pexpect.
    Commands are sent via sendline() and output is captured by waiting for the
    Vivado prompt (Vivado%). The session stays alive between commands, maintaining
    state (open projects, synthesized designs, etc.).

Key Design Decisions:
    1. Singleton Pattern: A global _session instance is used to ensure only one
       Vivado process runs at a time. Use get_session() to access it.

    2. Thread Safety: A threading lock protects command execution to prevent
       interleaved commands if multiple async tasks try to use Vivado.

    3. Prompt-Based Parsing: We wait for "Vivado%" prompt to know when a command
       completes. Output between command send and prompt is captured.

    4. Error Detection: Success/failure is determined by checking for error
       keywords in the output (ERROR:, invalid command, etc.).

    5. Statistics Tracking: Command count, timing, and error counts are tracked
       for debugging and performance analysis.

Usage:
    from vivado_session import get_session

    session = get_session()
    session.start()  # Launch Vivado

    result = session.run_tcl("open_project /path/to/project.xpr")
    if result.success:
        print(f"Project opened in {result.elapsed_ms}ms")

    session.stop()  # Clean shutdown

Dependencies:
    - pexpect: For spawning and interacting with Vivado process
    - Vivado: Must be installed and in PATH (or specify path explicitly)

Author: Created with Claude (Anthropic)
License: MIT
"""

import pexpect
import time
import re
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import threading


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class CommandResult:
    """
    Result from executing a Vivado TCL command.

    This dataclass encapsulates all information about a command execution,
    making it easy to check success, access output, and measure performance.

    Attributes:
        command: The TCL command that was executed
        output: The captured output from Vivado (excluding prompts)
        return_value: "0" for success, "1" for failure (string for JSON compat)
        success: Boolean indicating if the command succeeded
        elapsed_ms: Time taken to execute the command in milliseconds
        timestamp: ISO format timestamp of when the command completed

    Example:
        result = session.run_tcl("get_property PART [current_project]")
        if result.success:
            print(f"Target part: {result.output}")
            print(f"Took {result.elapsed_ms:.1f}ms")
    """
    command: str
    output: str
    return_value: str
    success: bool
    elapsed_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ErrorClassification:
    """
    Classification of Vivado output for smart error detection.

    Distinguishes between actual errors (TCL syntax/runtime errors, Vivado tool
    errors) and false positives (error strings that appear in report output like
    "Timing ERROR: 0" or utilization tables).

    Attributes:
        is_tcl_error: True if TCL syntax or runtime error detected
        is_vivado_error: True if Vivado tool error (lines starting with ERROR:)
        is_report_content: True if output appears to be report/table data
        error_messages: List of actual error message strings found
    """
    is_tcl_error: bool = False
    is_vivado_error: bool = False
    is_report_content: bool = False
    error_messages: list = field(default_factory=list)

    @property
    def is_actual_failure(self) -> bool:
        """Return True only if this is a real error, not report content."""
        return self.is_tcl_error or self.is_vivado_error


def classify_output_errors(output: str, command: str) -> ErrorClassification:
    """
    Classify errors based on context - distinguishes real failures from
    report content that happens to contain 'error' strings.

    This function performs smart error detection by:
    1. Checking for TCL syntax errors at the START of output
    2. Looking for Vivado errors that START with "ERROR:" (not just contain it)
    3. Detecting report context (tables, summaries) where "error" is just data

    Args:
        output: The raw output from Vivado
        command: The command that was executed (for context)

    Returns:
        ErrorClassification with details about any errors found

    Example:
        # This is a real error:
        # "ERROR: [Synth 8-87] can't read file..."

        # This is NOT an error (report content):
        # "| Timing ERROR      |  0  |"
        # "WNS(ns): -0.5  TNS ERROR: 0"
    """
    classification = ErrorClassification()
    lines = output.strip().split('\n')

    # TCL syntax errors - appear at START of output (first few lines)
    tcl_error_patterns = [
        r'^invalid command name',
        r'^wrong # args:',
        r'^can\'t read ".*": no such variable',
        r'^expected .* but got',
        r'^couldn\'t open',
        r'^no files matched',
    ]

    for line in lines[:5]:
        stripped = line.strip()
        for pattern in tcl_error_patterns:
            if re.match(pattern, stripped, re.IGNORECASE):
                classification.is_tcl_error = True
                classification.error_messages.append(stripped)

    # Vivado errors - lines STARTING with "ERROR:" followed by bracket
    # Real errors look like: "ERROR: [Synth 8-87] description"
    # False positives look like: "| Timing ERROR | 0 |" or "error: 0"
    for line in lines:
        stripped = line.strip()
        # Match lines that START with ERROR: followed by a bracket (Vivado error code)
        if re.match(r'^ERROR:\s*\[', stripped):
            classification.is_vivado_error = True
            classification.error_messages.append(stripped)

    # Detect report context - error strings in tables/summaries don't count as errors
    # These indicators suggest we're looking at report output, not error messages
    report_indicators = [
        'WNS(ns)',           # Timing summary
        'TNS(ns)',           # Timing summary
        'WHS(ns)',           # Timing summary
        '+---------',        # Table borders
        '|------',           # Table borders
        '| Site Type',       # Utilization report
        '| Resource',        # Utilization report
        'Utilization',       # Utilization report header
        'Design Timing Summary',
        'Clock Summary',
    ]
    if any(ind in output for ind in report_indicators):
        classification.is_report_content = True

    return classification


# =============================================================================
# VIVADO SESSION CLASS
# =============================================================================

class VivadoSession:
    """
    Manages a persistent Vivado TCL session using pexpect.

    Vivado is started once and kept running. Commands are sent and output
    is captured using pexpect's expect/sendline interface. This avoids the
    ~30 second startup time that would be incurred for each command.

    The session maintains state between commands, so you can open a project,
    run synthesis, and then query results - all using the same Vivado instance.

    Attributes:
        vivado_path: Path to the Vivado executable
        timeout: Maximum time to wait for command completion (seconds)
        child: The pexpect spawn object (Vivado process)
        is_running: Whether Vivado is currently running
        current_project: Path to currently open project (if any)
        stats: Dictionary of session statistics

    Thread Safety:
        A lock (_lock) protects command execution. Multiple threads can
        safely call run_tcl(), though commands will be serialized.

    Example:
        with VivadoSession() as session:
            session.run_tcl("open_project /path/to/project.xpr")
            result = session.run_tcl("report_timing_summary -return_string")
            print(result.output)
        # Vivado is automatically stopped when exiting the context
    """

    # Unique marker that won't appear in normal Vivado output
    # Used internally for sentinel-based parsing (not currently used but reserved)
    SENTINEL = "XYZZY_MCP_9f8e7d6c_DONE"

    def __init__(self, vivado_path: str = "vivado", timeout: float = 300.0):
        """
        Initialize the Vivado session manager.

        Args:
            vivado_path: Path to Vivado executable. Defaults to "vivado" which
                        assumes it's in the system PATH. Can be an absolute path
                        like "/tools/Xilinx/Vivado/2023.2/bin/vivado".
            timeout: Maximum time in seconds to wait for any command to complete.
                    Defaults to 300s (5 minutes) to handle long operations like
                    synthesis and implementation.
        """
        self.vivado_path = vivado_path
        self.timeout = timeout
        self.child: Optional[pexpect.spawn] = None
        self.is_running = False
        self.current_project: Optional[str] = None

        # Thread lock for command execution
        # Ensures only one command runs at a time even with async callers
        self._lock = threading.Lock()

        # Statistics tracking for debugging and performance analysis
        self.stats = {
            "session_start": None,       # ISO timestamp when session started
            "commands_run": 0,           # Total commands executed
            "total_command_time_ms": 0,  # Sum of all command times
            "errors": 0,                 # Count of failed commands
            "command_history": []        # Last 100 commands (for debugging)
        }

    def start(self) -> CommandResult:
        """
        Start the Vivado TCL session.

        This spawns a new Vivado process in TCL mode with:
        - No journal file (-nojournal): Avoids cluttering directory
        - No log file (-nolog): Output goes to pexpect instead

        The function waits for Vivado's startup banner ("Start of session")
        and then confirms readiness by waiting for the "Vivado%" prompt.

        Returns:
            CommandResult with success=True if Vivado started successfully,
            or success=False with error message if startup failed.

        Note:
            If already running, returns success immediately without restarting.
            Vivado startup typically takes 20-30 seconds.
        """
        # Don't restart if already running
        if self.is_running:
            return CommandResult(
                command="start",
                output="Session already running",
                return_value="0",
                success=True,
                elapsed_ms=0
            )

        start_time = time.time()

        try:
            # Spawn Vivado in TCL mode
            # -mode tcl: Interactive TCL shell (no GUI)
            # -nojournal: Don't create vivado.jou files
            # -nolog: Don't create vivado.log files
            self.child = pexpect.spawn(
                f'{self.vivado_path} -mode tcl -nojournal -nolog',
                encoding='utf-8',
                timeout=self.timeout,
                echo=False  # Don't echo commands back to us
            )

            # Wait for Vivado to display its startup banner
            # This indicates Vivado has loaded and is ready to accept commands
            self.child.expect('Start of session', timeout=120)

            # Brief pause to let Vivado fully initialize
            time.sleep(1)

            # Drain any remaining startup output to clear the buffer
            try:
                self.child.read_nonblocking(size=100000, timeout=1)
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass  # Expected - no more data to read

            # Send empty command to confirm we get a prompt back
            # This validates that Vivado is responsive
            self.child.sendline("")
            self.child.expect('Vivado%', timeout=10)

            # Mark session as running and record start time
            self.is_running = True
            self.stats["session_start"] = datetime.now().isoformat()

            elapsed = (time.time() - start_time) * 1000

            return CommandResult(
                command="start",
                output="Vivado session started successfully",
                return_value="0",
                success=True,
                elapsed_ms=elapsed
            )

        except pexpect.TIMEOUT:
            # Vivado didn't respond in time
            self.is_running = False
            elapsed = (time.time() - start_time) * 1000
            return CommandResult(
                command="start",
                output="Failed to start Vivado: Timeout waiting for startup",
                return_value="1",
                success=False,
                elapsed_ms=elapsed
            )
        except Exception as e:
            # Other errors (file not found, permissions, etc.)
            self.is_running = False
            elapsed = (time.time() - start_time) * 1000
            return CommandResult(
                command="start",
                output=f"Failed to start Vivado: {str(e)}",
                return_value="1",
                success=False,
                elapsed_ms=elapsed
            )

    def run_tcl(self, command: str, timeout_override: float = None) -> CommandResult:
        """
        Execute a TCL command and return the result.

        This is the primary interface for interacting with Vivado. The command
        is sent to the Vivado TCL shell, and output is captured by waiting for
        the next "Vivado%" prompt.

        Args:
            command: TCL command to execute. Can be any valid Vivado TCL command.
                    Examples:
                    - "open_project /path/to/project.xpr"
                    - "report_timing_summary -return_string"
                    - "get_property PART [current_project]"
            timeout_override: Optional timeout in seconds for this specific command.
                    Useful for long-running operations like synthesis (30+ min)
                    or implementation (60+ min). If None, uses self.timeout.

        Returns:
            CommandResult containing:
            - output: The command's output (stdout from Vivado)
            - success: True if no actual error was detected
            - elapsed_ms: Execution time in milliseconds

        Thread Safety:
            This method is thread-safe. A lock ensures only one command
            executes at a time.

        Output Parsing:
            The raw pexpect output includes the echoed command and prompts.
            This method strips those to return only the meaningful output.

        Error Detection:
            Uses smart error classification to distinguish real errors from
            report content that contains error-like strings. Real errors are:
            - TCL syntax errors at start of output
            - Vivado errors (lines starting with "ERROR: [code]")
            Report content like "Timing ERROR: 0" is NOT treated as an error.
        """
        # Check session is running
        if not self.is_running:
            return CommandResult(
                command=command,
                output="Vivado session not running. Call start() first.",
                return_value="1",
                success=False,
                elapsed_ms=0
            )

        # Serialize command execution with a lock
        with self._lock:
            start_time = time.time()

            try:
                # Clear any pending output from previous commands
                # This ensures we only capture this command's output
                try:
                    self.child.read_nonblocking(size=100000, timeout=0.1)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass  # Expected - buffer was empty

                # Send the command to Vivado
                self.child.sendline(command)

                # Wait for the Vivado prompt indicating command completion
                # The prompt appears after Vivado finishes processing
                # Use timeout_override if provided (for long operations like synthesis)
                effective_timeout = timeout_override if timeout_override is not None else self.timeout
                self.child.expect('Vivado%', timeout=effective_timeout)

                # Get everything that was output before the prompt
                raw_output = self.child.before

                # Parse the output to extract meaningful content
                # Raw output includes: command echo, actual output, whitespace
                lines = raw_output.replace('\r', '').split('\n')
                clean_lines = []
                found_command = False

                # Normalize command for matching (handle whitespace differences)
                cmd_normalized = command.strip()

                for line in lines:
                    stripped = line.strip()

                    # Skip lines until we find the echoed command
                    # Everything before is leftover from previous operations
                    if not found_command:
                        if cmd_normalized in stripped:
                            found_command = True
                        continue

                    # Skip Vivado prompts in output
                    if stripped == 'Vivado%' or stripped.startswith('Vivado%'):
                        continue

                    # Skip empty lines for cleaner output
                    if not stripped:
                        continue

                    clean_lines.append(stripped)

                output = '\n'.join(clean_lines).strip()

                elapsed = (time.time() - start_time) * 1000

                # Use smart error classification to detect real errors
                # This avoids false positives from report content like "Timing ERROR: 0"
                classification = classify_output_errors(output, command)
                success = not classification.is_actual_failure

                # Update statistics
                self.stats["commands_run"] += 1
                self.stats["total_command_time_ms"] += elapsed
                if not success:
                    self.stats["errors"] += 1

                result = CommandResult(
                    command=command,
                    output=output,
                    return_value="0" if success else "1",
                    success=success,
                    elapsed_ms=elapsed
                )

                # Add to command history (keep last 100 for debugging)
                self.stats["command_history"].append({
                    "command": command,
                    "success": success,
                    "elapsed_ms": elapsed,
                    "timestamp": result.timestamp
                })
                if len(self.stats["command_history"]) > 100:
                    self.stats["command_history"] = self.stats["command_history"][-100:]

                return result

            except pexpect.TIMEOUT:
                # Command took too long - might be hung or very long operation
                elapsed = (time.time() - start_time) * 1000
                self.stats["errors"] += 1
                return CommandResult(
                    command=command,
                    output=f"Command timed out after {self.timeout}s",
                    return_value="1",
                    success=False,
                    elapsed_ms=elapsed
                )
            except Exception as e:
                # Unexpected error during command execution
                elapsed = (time.time() - start_time) * 1000
                self.stats["errors"] += 1
                return CommandResult(
                    command=command,
                    output=f"Error executing command: {str(e)}",
                    return_value="1",
                    success=False,
                    elapsed_ms=elapsed
                )

    def stop(self) -> CommandResult:
        """
        Stop the Vivado session gracefully.

        Sends the "exit" command to Vivado and waits for the process to
        terminate. If graceful exit fails, force-closes the process.

        Returns:
            CommandResult with success=True (stopping always "succeeds"
            even if we had to force-close)

        Note:
            Safe to call even if session is not running.
        """
        if not self.is_running:
            return CommandResult(
                command="stop",
                output="Session not running",
                return_value="0",
                success=True,
                elapsed_ms=0
            )

        start_time = time.time()

        try:
            # Send exit command for graceful shutdown
            self.child.sendline('exit')
            # Wait for process to terminate (EOF on stdout)
            self.child.expect(pexpect.EOF, timeout=30)
        except Exception:
            # If graceful exit fails, force-terminate the process
            try:
                self.child.close(force=True)
            except:
                pass  # Best effort - process might already be dead

        # Update state
        self.is_running = False
        self.current_project = None
        elapsed = (time.time() - start_time) * 1000

        return CommandResult(
            command="stop",
            output="Vivado session stopped",
            return_value="0",
            success=True,
            elapsed_ms=elapsed
        )

    def get_stats(self) -> dict:
        """
        Get session statistics for monitoring and debugging.

        Returns:
            Dictionary containing:
            - is_running: Whether session is active
            - current_project: Path to open project (or None)
            - session_start: ISO timestamp when session started
            - commands_run: Total commands executed
            - total_command_time_ms: Sum of all command times
            - errors: Count of failed commands
            - avg_command_time_ms: Average command time (if commands > 0)
            - command_history: Last 100 commands with timing info
        """
        stats = self.stats.copy()
        stats["is_running"] = self.is_running
        stats["current_project"] = self.current_project

        # Calculate average command time if we have data
        if self.stats["commands_run"] > 0:
            stats["avg_command_time_ms"] = (
                self.stats["total_command_time_ms"] / self.stats["commands_run"]
            )

        return stats

    def is_healthy(self) -> bool:
        """
        Check if the Vivado session is responsive.

        Sends a simple command to Vivado and checks if it responds within
        a short timeout. This is useful for detecting hung or dead sessions.

        Returns:
            True if session responds, False if unresponsive or not running

        Note:
            This is a quick check (5 second timeout). Use ensure_healthy()
            if you want to automatically recover from unhealthy sessions.
        """
        if not self.is_running or not self.child:
            return False
        try:
            # Send a simple command that produces predictable output
            self.child.sendline("puts {HEALTH_OK}")
            self.child.expect("HEALTH_OK", timeout=5)
            self.child.expect("Vivado%", timeout=5)
            return True
        except (pexpect.TIMEOUT, pexpect.EOF):
            return False

    def ensure_healthy(self) -> CommandResult:
        """
        Check session health and restart if needed.

        This is the recommended way to recover from session failures.
        It checks if the session is responsive and automatically restarts
        it if not.

        Returns:
            CommandResult indicating health status or restart result

        Example:
            result = session.ensure_healthy()
            if result.success:
                # Session is ready to use
                session.run_tcl("...")
        """
        if self.is_healthy():
            return CommandResult(
                command="health_check",
                output="Session healthy",
                return_value="0",
                success=True,
                elapsed_ms=0
            )
        # Session is unhealthy, try to restart
        self.stop()
        return self.start()

    def __enter__(self):
        """
        Context manager entry - start the session.

        Example:
            with VivadoSession() as session:
                session.run_tcl("...")
        """
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit - stop the session.

        Ensures Vivado is properly shut down even if an exception occurred.
        """
        self.stop()


# =============================================================================
# SINGLETON SESSION MANAGEMENT
# =============================================================================

# Global singleton session instance
# Using a singleton ensures only one Vivado process runs at a time
_session: Optional[VivadoSession] = None


def get_session() -> VivadoSession:
    """
    Get or create the global Vivado session.

    This function implements the singleton pattern for VivadoSession.
    The first call creates a new session; subsequent calls return the
    same instance.

    Returns:
        The global VivadoSession instance

    Example:
        session = get_session()
        session.start()
        # ... use session ...
        session.stop()

    Note:
        The session is created lazily (on first access) and is NOT
        automatically started. Call session.start() explicitly.
    """
    global _session
    if _session is None:
        _session = VivadoSession()
    return _session


def reset_session():
    """
    Reset the global session (stop if running and clear instance).

    Use this to force a fresh Vivado session, for example after
    recovering from an error or when changing Vivado versions.

    This function:
    1. Stops the current session if running
    2. Clears the singleton instance

    The next call to get_session() will create a fresh instance.
    """
    global _session
    if _session is not None and _session.is_running:
        _session.stop()
    _session = None
