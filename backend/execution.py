"""Bounded subprocess execution for trusted snippets; this is not a security sandbox."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limit_reached: bool = False


def _stop_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def run_process(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: float,
    max_memory_mb: int,
    max_output_bytes: int,
    max_stderr_bytes: int | None = None,
) -> ProcessResult:
    """Capture to bounded files and stop the process group on timeout or excess output."""
    stderr_limit = max_stderr_bytes or max_output_bytes
    file_limit = max(4 * 1024 * 1024, max_output_bytes, stderr_limit)
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    for name in ("SYSTEMROOT", "WINDIR"):
        if os.name == "nt" and name in os.environ:
            environment[name] = os.environ[name]

    launcher = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        str(max_memory_mb),
        str(file_limit),
        str(timeout_seconds),
        *command,
    ]
    timed_out = False
    output_limit_reached = False
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        with subprocess.Popen(
            launcher,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=os.name == "posix",
        ) as process:
            deadline = time.monotonic() + timeout_seconds
            try:
                while process.poll() is None:
                    if (
                        os.fstat(stdout_file.fileno()).st_size > max_output_bytes
                        or os.fstat(stderr_file.fileno()).st_size > stderr_limit
                    ):
                        output_limit_reached = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(0.01)
            finally:
                # Stop descendants that belong to the request's process group.
                _stop_process_group(process)
                process.wait()

            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            output_limit_reached = output_limit_reached or (
                stdout_size > max_output_bytes
                or stderr_size > stderr_limit
                or (
                    os.name == "posix"
                    and process.returncode != 0
                    and (stdout_size >= file_limit or stderr_size >= file_limit)
                )
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            return ProcessResult(
                returncode=process.returncode,
                stdout=stdout_file.read(max_output_bytes).decode("utf-8", errors="ignore"),
                stderr=stderr_file.read(stderr_limit).decode("utf-8", errors="ignore"),
                timed_out=timed_out,
                output_limit_reached=output_limit_reached,
            )


def _launch_limited_process() -> None:
    memory_mb, file_bytes = int(sys.argv[1]), int(sys.argv[2])
    timeout_seconds = float(sys.argv[3])
    if os.name == "posix":
        import resource

        limits = (
            (resource.RLIMIT_AS, memory_mb * 1024 * 1024),
            (resource.RLIMIT_FSIZE, file_bytes),
            (resource.RLIMIT_CPU, max(1, math.ceil(timeout_seconds) + 1)),
            (resource.RLIMIT_CORE, 0),
        )
        for kind, limit in limits:
            _soft, hard = resource.getrlimit(kind)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(kind, (limit, limit))
    os.execv(sys.argv[4], sys.argv[4:])


if __name__ == "__main__":
    _launch_limited_process()
