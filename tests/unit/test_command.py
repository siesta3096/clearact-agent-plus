import asyncio
import sys

import pytest

from clearact.domain.errors import ToolValidationError
from clearact.tools.base import ToolContext
from clearact.tools.command import ExecuteCommandTool


def execute(workspace, code, **arguments):
    return asyncio.run(ExecuteCommandTool().execute(
        {"command": [sys.executable, "-c", code], **arguments}, ToolContext(workspace), "command_test",
    ))


def test_command_runs_in_workspace_and_preserves_stdout_stderr_and_exit_code(workspace):
    result = execute(workspace, "import sys; from pathlib import Path; print(Path.cwd()); "
                     "print('error detail', file=sys.stderr); sys.exit(3)")
    assert result.ok is False
    assert result.metadata["exit_code"] == 3
    assert str(workspace) in result.metadata["stdout"]
    assert "error detail" in result.metadata["stderr"]


def test_command_does_not_interpret_shell_metacharacters(workspace):
    text = "hello; $(echo secret) & value with spaces"
    result = asyncio.run(ExecuteCommandTool().execute(
        {"command": [sys.executable, "-c", "import sys; print(sys.argv[1])", text]},
        ToolContext(workspace), "literal",
    ))
    assert result.ok
    assert result.metadata["stdout"].strip() == text


def test_python_command_preserves_unicode_output(workspace):
    text = "中文输出：文件已生成"
    result = execute(workspace, f"print({text!r})")
    assert result.ok
    assert result.metadata["stdout"].strip() == text


def test_command_output_is_bounded_without_deadlocking(workspace):
    result = execute(workspace, "import sys; print('x' * 200000); print('y' * 200000, file=sys.stderr)",
                     max_output_chars=256)
    assert result.ok
    assert result.metadata["output_truncated"]
    assert len(result.metadata["stdout"]) <= 256
    assert len(result.metadata["stderr"]) <= 256


def test_command_timeout_terminates_the_process(workspace):
    result = execute(workspace, "import time; time.sleep(10)", timeout_seconds=0.1)
    assert not result.ok
    assert result.metadata["timed_out"]
    assert result.metadata["exit_code"] is not None


def test_command_cancellation_terminates_spawned_process(workspace):
    marker = workspace / "late.txt"

    async def exercise():
        task = asyncio.create_task(ExecuteCommandTool().execute(
            {"command": [sys.executable, "-c", "import time; from pathlib import Path; "
                         "time.sleep(1); Path('late.txt').write_text('still alive')"]},
            ToolContext(workspace), "cancel",
        ))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.1)

    asyncio.run(exercise())
    assert not marker.exists()


def test_command_cleans_up_background_children_after_parent_exits(workspace):
    marker = workspace / "child-survived.txt"
    child_code = "import time; from pathlib import Path; time.sleep(1.2); " \
                 "Path('child-survived.txt').write_text('child still running')"
    parent_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print('parent exited', flush=True)"
    )

    async def exercise():
        result = await ExecuteCommandTool().execute(
            {"command": [sys.executable, "-c", parent_code], "timeout_seconds": 0.5},
            ToolContext(workspace), "orphaned_child",
        )
        # Commands cannot leave detached background work running. A Windows job
        # may clean it up at parent exit; a POSIX process group may need timeout
        # when the child inherits output pipes. Wait for its finite lifetime
        # even on failure, avoiding leaks.
        await asyncio.sleep(1.4)
        return result

    result = asyncio.run(exercise())

    assert result.metadata["exit_code"] is not None
    assert "parent exited" in result.metadata["stdout"]
    assert not marker.exists()


@pytest.mark.parametrize("encoding", ["base64_codec", "hex_codec", "rot_13"])
def test_command_rejects_non_text_codecs_before_starting_process(workspace, encoding):
    marker = workspace / "unexpected-command.txt"

    with pytest.raises(ToolValidationError, match="encoding"):
        execute(workspace, "from pathlib import Path; Path('unexpected-command.txt').write_text('ran')",
                encoding=encoding)

    assert not marker.exists()


@pytest.mark.parametrize("arguments", [
    {"command": "python --version"}, {"command": []}, {"command": ["python", "\x00"]},
    {"command": ["python"], "cwd": ".."}, {"command": ["python"], "timeout_seconds": 0},
])
def test_command_rejects_invalid_input_and_working_directory(workspace, arguments):
    with pytest.raises(ToolValidationError):
        asyncio.run(ExecuteCommandTool().execute(arguments, ToolContext(workspace), "invalid"))
