"""Parent-side timeout and stable result mapping for script-policy preflight."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .ablation import PreflightResult

_TIMEOUT_S = 10
_KNOWN_CODES = frozenset(
    {"SCRIPT_SYNTAX", "SCRIPT_IMPORT", "FACTORY_MISSING", "POLICY_INVALID", "COMMAND_INVALID"}
)


def _worker_command(path: Path, *, root: Path | None = None) -> list[str]:
    """Build the fixed, isolated module invocation for a candidate policy."""
    if root is not None:
        return [
            sys.executable,
            "-I",
            str(root.resolve() / "src" / "aisle" / "harness" / "script_preflight_worker.py"),
            str(path.resolve()),
        ]
    return [
        sys.executable,
        "-I",
        "-m",
        "aisle.harness.script_preflight_worker",
        str(path.resolve()),
    ]


def _failure(code: str, started: float) -> PreflightResult:
    return PreflightResult(ok=False, errors=({"code": code},), wall_s=time.monotonic() - started)


def preflight_script(path: Path, *, root: Path | None = None) -> PreflightResult:
    """Preflight a candidate without importing it into the harness process."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _worker_command(path, root=root),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _failure("POLICY_INVALID", started)
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _failure("POLICY_INVALID", started)
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return _failure("POLICY_INVALID", started)
    if response["ok"] and completed.returncode == 0 and set(response) == {"ok"}:
        return PreflightResult(ok=True, errors=(), wall_s=time.monotonic() - started)
    code = response.get("code")
    if isinstance(code, str) and code in _KNOWN_CODES:
        return _failure(code, started)
    return _failure("POLICY_INVALID", started)
