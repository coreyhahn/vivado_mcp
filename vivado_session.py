"""Vivado TCL session manager - maintains persistent Vivado process using pexpect."""

import pexpect
import time
import re
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import threading


@dataclass
class CommandResult:
    """Result from a Vivado TCL command."""
    command: str
    output: str
    return_value: str
    success: bool
    elapsed_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class VivadoSession:
    """
    Manages a persistent Vivado TCL session using pexpect.

    Vivado is started once and kept running. Commands are sent and output
    is captured using pexpect's expect/sendline interface.
    """

    # Unique marker to detect end of command output (use something that won't appear in normal output)
    SENTINEL = "XYZZY_MCP_9f8e7d6c_DONE"

    def __init__(self, vivado_path: str = "vivado", timeout: float = 300.0):
        self.vivado_path = vivado_path
        self.timeout = timeout
        self.child: Optional[pexpect.spawn] = None
        self.is_running = False
        self.current_project: Optional[str] = None
        self._lock = threading.Lock()

        # Statistics
        self.stats = {
            "session_start": None,
            "commands_run": 0,
            "total_command_time_ms": 0,
            "errors": 0,
            "command_history": []
        }

    def start(self) -> CommandResult:
        """Start the Vivado TCL session."""
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
            # Start Vivado with pexpect
            self.child = pexpect.spawn(
                f'{self.vivado_path} -mode tcl -nojournal -nolog',
                encoding='utf-8',
                timeout=self.timeout,
                echo=False  # Don't echo commands back
            )

            # Wait for Vivado to start (look for startup banner)
            self.child.expect('Start of session', timeout=120)

            # Give it a moment to fully initialize
            time.sleep(1)

            # Drain any remaining startup output
            try:
                self.child.read_nonblocking(size=100000, timeout=1)
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass

            # Wait for prompt to confirm ready
            self.child.sendline("")  # Empty command to get prompt
            self.child.expect('Vivado%', timeout=10)

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
            self.is_running = False
            elapsed = (time.time() - start_time) * 1000
            return CommandResult(
                command="start",
                output=f"Failed to start Vivado: {str(e)}",
                return_value="1",
                success=False,
                elapsed_ms=elapsed
            )

    def run_tcl(self, command: str) -> CommandResult:
        """
        Execute a TCL command and return the result.

        Args:
            command: TCL command to execute

        Returns:
            CommandResult with output and status
        """
        if not self.is_running:
            return CommandResult(
                command=command,
                output="Vivado session not running. Call start() first.",
                return_value="1",
                success=False,
                elapsed_ms=0
            )

        with self._lock:
            start_time = time.time()

            try:
                # Clear any pending output first
                try:
                    self.child.read_nonblocking(size=100000, timeout=0.1)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass

                # Send the command
                self.child.sendline(command)

                # Wait for Vivado prompt (indicates command completed)
                self.child.expect('Vivado%', timeout=self.timeout)

                # Get the output (everything before the prompt)
                raw_output = self.child.before

                # Parse output: extract content after command echo
                lines = raw_output.replace('\r', '').split('\n')
                clean_lines = []
                found_command = False

                # Normalize command for matching
                cmd_normalized = command.strip()

                for line in lines:
                    stripped = line.strip()

                    # Look for the command echo
                    if not found_command:
                        if cmd_normalized in stripped:
                            found_command = True
                        continue

                    # Skip Vivado prompts
                    if stripped == 'Vivado%' or stripped.startswith('Vivado%'):
                        continue

                    # Skip empty lines
                    if not stripped:
                        continue

                    clean_lines.append(stripped)

                output = '\n'.join(clean_lines).strip()

                elapsed = (time.time() - start_time) * 1000

                # Check for errors in output
                success = not any(err in output.lower() for err in
                                  ["error:", "invalid command", "can't read", "wrong # args"])

                # Update stats
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

                # Keep last 100 commands in history
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
        """Stop the Vivado session."""
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
            self.child.sendline('exit')
            self.child.expect(pexpect.EOF, timeout=30)
        except Exception:
            # Force close if graceful exit fails
            try:
                self.child.close(force=True)
            except:
                pass

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
        """Get session statistics."""
        stats = self.stats.copy()
        stats["is_running"] = self.is_running
        stats["current_project"] = self.current_project
        if self.stats["commands_run"] > 0:
            stats["avg_command_time_ms"] = self.stats["total_command_time_ms"] / self.stats["commands_run"]
        return stats

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


# Singleton session instance
_session: Optional[VivadoSession] = None


def get_session() -> VivadoSession:
    """Get or create the global Vivado session."""
    global _session
    if _session is None:
        _session = VivadoSession()
    return _session


def reset_session():
    """Reset the global session (stop if running)."""
    global _session
    if _session is not None and _session.is_running:
        _session.stop()
    _session = None
