from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from clearact.domain.errors import ToolValidationError
from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.base import ToolContext


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    """Clean up a spawned command on cancellation, including its children."""
    if os.name == "nt":
        # Windows worker owns a kill-on-close job containing all descendants.
        if process.returncode is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


class ExecuteCommandTool:
    name = "execute_command"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Run an executable and arguments in the authorized workspace after explicit approval. "
                "Use an argv array, e.g. ['python', '--version']; no implicit shell. "
                "For shell syntax explicitly invoke powershell/cmd/bash. Commands are NOT sandboxed: "
                "they may access files or network outside cwd, and their effects cannot be rolled back. "
                "Returns stdout/stderr, exit code, timeout and truncation status."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
                    "cwd": {"type": "string", "description": "Directory within the authorized workspace."},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 300},
                    "max_output_chars": {"type": "integer", "minimum": 256, "maximum": 100000,
                                         "description": "Maximum per stream; default 2000 to preserve context budget."},
                    "encoding": {"type": "string", "description": "Output encoding; default utf-8."},
                },
                "required": ["command"],
            },
        )

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, list) or not 1 <= len(command) <= 128 or any(
            not isinstance(part, str) or "\x00" in part for part in command
        ) or not command[0].strip() or sum(map(len, command)) > 32000:
            raise ToolValidationError("command must be a bounded, non-empty array of executable and arguments")
        raw_cwd = arguments.get("cwd", ".")
        if not isinstance(raw_cwd, str) or "\x00" in raw_cwd:
            raise ToolValidationError("cwd must be a directory path")
        candidate = Path(raw_cwd).expanduser()
        cwd = (candidate if candidate.is_absolute() else context.workspace_root / candidate).resolve()
        if not cwd.is_relative_to(context.workspace_root) or not cwd.is_dir():
            raise ToolValidationError("cwd must be an existing directory within the authorized workspace")
        timeout = arguments.get("timeout_seconds", 60)
        output_limit = arguments.get("max_output_chars", 2000)
        if type(timeout) not in {int, float} or not 0.1 <= timeout <= 300:
            raise ToolValidationError("timeout_seconds must be between 0.1 and 300")
        if type(output_limit) is not int or not 256 <= output_limit <= 100000:
            raise ToolValidationError("max_output_chars must be between 256 and 100000")
        encoding = arguments.get("encoding", "utf-8")
        try:
            if not isinstance(encoding, str):
                raise LookupError
            codecs.lookup(encoding)
            b"x".decode(encoding, errors="replace")
        except (LookupError, UnicodeError, ValueError) as exc:
            raise ToolValidationError("encoding must be a supported text encoding") from exc

        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        invocation = (
            [sys.executable, str(Path(__file__).with_name("_command_worker.py")),
             json.dumps(command, ensure_ascii=False)] if os.name == "nt" else command
        )
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = encoding
        try:
            process = await asyncio.create_subprocess_exec(
                *invocation, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, **options,
            )
        except OSError as exc:
            return ToolResult(
                action_id=action_id, tool_name=self.name, ok=False,
                content=f"Could not start command: {exc}",
                metadata={"command": command, "cwd": str(cwd), "exit_code": None, "side_effects": "none"},
            )

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}

        async def drain(name: str, stream: asyncio.StreamReader) -> None:
            while chunk := await stream.read(8192):
                available = output_limit * 4 - len(buffers[name])
                buffers[name].extend(chunk[:max(0, available)])
                truncated[name] |= len(chunk) > available

        readers = [asyncio.create_task(drain("stdout", process.stdout)),
                   asyncio.create_task(drain("stderr", process.stderr))]

        async def finish() -> None:
            await process.wait()
            await asyncio.gather(*readers)

        timed_out = False
        try:
            await asyncio.wait_for(finish(), timeout=float(timeout))
            if os.name != "nt":
                await _terminate_tree(process)  # Clean up background descendants on normal completion too.
        except TimeoutError:
            timed_out = True
            await _terminate_tree(process)
        except asyncio.CancelledError:
            await _terminate_tree(process)
            raise
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        outputs = {}
        for name, data in buffers.items():
            decoded = data.decode(encoding, errors="replace")
            truncated[name] |= len(decoded) > output_limit
            outputs[name] = decoded[:output_limit]
        status = "timed out" if timed_out else f"exited with code {process.returncode}"
        suffix = "\nOutput was truncated." if any(truncated.values()) else ""
        content = f"Command {status}.\nstdout:\n{outputs['stdout']}\nstderr:\n{outputs['stderr']}{suffix}"
        return ToolResult(
            action_id=action_id, tool_name=self.name, ok=not timed_out and process.returncode == 0,
            content=content,
            metadata={"command": command, "cwd": str(cwd), "exit_code": process.returncode,
                      "stdout": outputs["stdout"], "stderr": outputs["stderr"], "timed_out": timed_out,
                      "timeout_seconds": timeout,
                      "output_truncated": any(truncated.values()), "side_effects": "unknown"},
        )
