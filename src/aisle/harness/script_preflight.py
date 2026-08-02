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


def _worker_command(path: Path) -> list[str]:
    """Build an isolated launcher that resolves only this worktree's worker."""
    source_root = Path(__file__).resolve().parents[2]
    launcher = (
        "import runpy,sys; "
        f"sys.path.insert(0, {str(source_root)!r}); "
        "sys.argv=['aisle.harness.script_preflight_worker', sys.argv[1]]; "
        "runpy.run_module('aisle.harness.script_preflight_worker', run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", launcher, str(path.resolve())]


def _failure(code: str, started: float) -> PreflightResult:
    return PreflightResult(ok=False, errors=({"code": code},), wall_s=time.monotonic() - started)


def preflight_script(path: Path) -> PreflightResult:
    """Preflight a candidate without importing it into the harness process."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _worker_command(path), capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
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
