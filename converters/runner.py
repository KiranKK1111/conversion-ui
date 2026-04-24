"""Subprocess runner with live line-by-line stdout streaming.

Used by the Streamlit UI to surface progress from the existing CLI scripts
without touching their internals. We spawn the script with unbuffered I/O
(`-u`) and merge stderr into stdout so tqdm progress bars (which write to
stderr by default) stream into the UI the same way."""

import subprocess
import sys
from pathlib import Path
from typing import Iterator


def run_script(args: list[str], cwd: Path | str | None = None) -> subprocess.Popen:
    """Spawn a Python script with unbuffered streaming I/O.

    Caller is responsible for iterating proc.stdout and calling proc.wait()."""
    proc = subprocess.Popen(
        [sys.executable, "-u", *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )
    return proc


def stream_lines(proc: subprocess.Popen) -> Iterator[str]:
    """Yield lines from a running subprocess until it exits. Closes stdout after."""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.stdout.close()
        proc.wait()
