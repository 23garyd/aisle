"""Isolated worker for script-policy import and contract preflight."""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


def _result(ok: bool, code: str | None = None) -> None:
    payload: dict[str, object] = {"ok": ok}
    if code is not None:
        payload["code"] = code
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_policy(path: Path):
    spec = importlib.util.spec_from_file_location("aisle_script_candidate", path)
    if spec is None or spec.loader is None:
        raise ImportError("candidate has no import loader")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commands_are_valid(commands: object) -> bool:
    from baselines.script_s1.contract import PolicyCommand

    return isinstance(commands, list) and all(
        isinstance(command, PolicyCommand) for command in commands
    )


def main(argv: list[str] | None = None) -> int:
    """Write exactly one JSON result for an absolute candidate policy path."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        _result(False, "POLICY_INVALID")
        return 1
    path = Path(args[0])
    if not path.is_absolute() or not path.is_file():
        _result(False, "SCRIPT_IMPORT")
        return 1

    # This is the fixed baseline API, never the candidate's directory.
    root = str(_repository_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        source = path.read_bytes()
        compile(source, str(path), "exec")
    except (OSError, SyntaxError):
        _result(False, "SCRIPT_SYNTAX")
        return 1
    try:
        with redirect_stdout(StringIO()):
            module = _load_policy(path)
    except Exception:
        _result(False, "SCRIPT_IMPORT")
        return 1

    with redirect_stdout(StringIO()):
        factory = getattr(module, "create_policy", None)
    if not callable(factory):
        _result(False, "FACTORY_MISSING")
        return 1
    try:
        with redirect_stdout(StringIO()):
            policy = factory(0)
    except Exception:
        _result(False, "POLICY_INVALID")
        return 1
    with redirect_stdout(StringIO()):
        handler = getattr(policy, "on_event", None)
    if not callable(handler):
        _result(False, "POLICY_INVALID")
        return 1

    try:
        from baselines.script_s1.contract import PolicyEvent

        with redirect_stdout(StringIO()):
            commands = handler(PolicyEvent("episode_goal", {}, 0))
    except ValueError:
        _result(False, "COMMAND_INVALID")
        return 1
    except Exception:
        _result(False, "POLICY_INVALID")
        return 1
    if not _commands_are_valid(commands):
        _result(False, "COMMAND_INVALID")
        return 1
    _result(True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
