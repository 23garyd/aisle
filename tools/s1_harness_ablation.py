#!/usr/bin/env python3
"""External authority for the randomized S1 harness-versus-script campaign.

The controller alone assigns conditions, starts coding-agent processes, meters
their live token streams and wall time, constrains the trusted rollout ledger,
and unlocks held-out scoring after the agent stops.  Every command follows
CON-8: exactly one JSON object on stdout, diagnostics on stderr, and exit zero
if and only if ``ok`` is true.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from campaign import UsageCounter, agent_cmd_campaign, audit_frozen  # noqa: E402

from aisle.harness.ablation import (  # noqa: E402
    AttemptResult,
    append_ledger,
    paired_assignments,
    sha256_file,
    verify_ledger,
)
from aisle.harness.ablation_adapters import AisleAdapter, ScriptAdapter  # noqa: E402
from aisle.harness.rollout import (  # noqa: E402
    budget_ledger,
    load_campaign_budget,
    reserve_budget,
    settle_budget,
)
from aisle.harness.rollout import (  # noqa: E402
    verify_ledger as verify_budget_ledger,
)

DEVELOPMENT_SEEDS = "0..49"
REGRESSION_SEEDS = "0..7"
HELDOUT_SEEDS = "100..107"
MAX_NEW_TOKENS = 500_000
MAX_DEVELOPMENT_EPISODES = 40
MAX_WALL_HOURS = 4.0
HELDOUT_EPISODES = 8

STARTERS = {
    "aisle": Path("graphs/ablation_s1_starter.yaml"),
    "script": Path("baselines/script_s1/starter.py"),
}
CANDIDATES = {
    "aisle": Path("graphs/agent_s1_ablation.yaml"),
    "script": Path("baselines/script_s1/candidate.py"),
}
ARTIFACT_NAMES = (
    "session.json",
    "attempts.jsonl",
    "agent.jsonl",
    "token_samples.jsonl",
    "holdout.json",
    "audit.json",
)
CONTROLLER_RESERVATION = "__s1_ablation_controller_reserve__"
GLOBAL_SIMULATOR_LOCK = Path("/tmp/aisle-s1-harness-ablation-simulator.lock")

_GOAL = (
    "Improve the supplied incomplete S1 store-order-pick starter so the robot "
    "delivers exactly the requested products to the counter. Maximize verified "
    "success without weakening reset, verifier, environment, or safety controls."
)
_INTERFACE = {
    "aisle": (
        "Edit only the assigned typed candidate graph and supporting non-frozen "
        "AISLE nodes or skills. Use the registry, validator, structured failures, "
        "and permitted per-node traces."
    ),
    "script": (
        "Edit only the assigned Python policy and supporting non-frozen Python "
        "helpers. Use its documented PolicyEvent/PolicyCommand API, preflight, "
        "structured policy log, and shared failure records."
    ),
}


class ControllerError(Exception):
    """A stable CLI refusal."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class TelemetryError(ValueError):
    """The live vendor stream cannot support trustworthy token accounting."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Turn argparse refusals into the controller's single-object contract."""

    def error(self, message: str) -> None:
        raise ControllerError("ARGUMENT", message)


@dataclass(frozen=True)
class ExecutionResult:
    stopped: str
    returncode: int
    wall_s: float
    agent_version: str


def _resolve_pin(repo: Path, pin: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{pin}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    oid = result.stdout.strip()
    if result.returncode != 0 or len(oid) != 40:
        raise ControllerError("PIN_INVALID", f"cannot resolve commit {pin!r}")
    return oid


def _create_worktree(pin: str, destination: Path) -> Path:
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), pin],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise ControllerError(
            "WORKTREE_CREATE",
            f"git worktree add failed: {(result.stderr or result.stdout).strip()[-500:]}",
        )
    return destination


def _worktree_head(worktree: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _worktree_status(worktree: Path) -> list[str] | None:
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def default_adapter_factory(condition: str, worktree: Path):
    if condition == "aisle":
        return AisleAdapter(worktree)
    return ScriptAdapter(root=worktree)


@dataclass(frozen=True)
class ControllerRuntime:
    """Narrow external boundaries; tests replace them with zero-cost fixtures."""

    resolve_pin: Callable[[Path, str], str] = _resolve_pin
    create_worktree: Callable[[str, Path], Path] = _create_worktree
    execute_agent: Callable[..., ExecutionResult] | None = None
    adapter_factory: Callable[[str, Path], Any] = default_adapter_factory
    audit_frozen: Callable[[Path, str], list[str]] = audit_frozen
    worktree_head: Callable[[Path], str | None] = _worktree_head
    worktree_status: Callable[[Path], list[str] | None] = _worktree_status
    epoch_time: Callable[[], float] = time.time


class AttemptBroker:
    """Authenticated controller-owned IPC for in-session adapter launches."""

    def __init__(self, session_dir: Path, runtime: ControllerRuntime) -> None:
        self.session_dir = session_dir.resolve()
        self.runtime = runtime
        self.capability = secrets.token_hex(32)
        nonce = secrets.token_hex(8)
        self.socket_path = Path("/tmp") / f"aisle-s1-attempt-{nonce}.sock"
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def capability_sha256(self) -> str:
        return _sha256_text(self.capability)

    def environment(self) -> dict[str, str]:
        return {
            "AISLE_ABLATION_SESSION": str(self.session_dir),
            "AISLE_ABLATION_CONTROLLER": str(Path(__file__).resolve()),
            "AISLE_ABLATION_SOCKET": str(self.socket_path),
            "AISLE_ABLATION_CAPABILITY": self.capability,
        }

    def __enter__(self) -> AttemptBroker:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(1)
        server.settimeout(0.1)
        self._server = server
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _response(self, request: object) -> dict:
        if (
            not isinstance(request, dict)
            or set(request) != {"capability", "session", "seeds"}
            or request.get("capability") != self.capability
            or Path(str(request.get("session"))).resolve() != self.session_dir
            or not isinstance(request.get("seeds"), str)
        ):
            return {
                "ok": False,
                "code": "ATTEMPT_BROKER_AUTH",
                "error": "attempt request was not authorized for this session",
            }
        try:
            return _attempt(
                argparse.Namespace(
                    session=str(self.session_dir),
                    seeds=request["seeds"],
                ),
                self.runtime,
            )
        except ControllerError as exc:
            return {"ok": False, "code": exc.code, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001 - preserve the broker response contract
            print(f"[s1-ablation] attempt broker error: {exc!r}", file=sys.stderr)
            return {"ok": False, "code": "INTERNAL", "error": str(exc)}

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    stream = connection.makefile("r", encoding="utf-8")
                    line = stream.readline(1024 * 1024)
                    request = json.loads(line)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    response = {
                        "ok": False,
                        "code": "ATTEMPT_BROKER_PROTOCOL",
                        "error": "attempt request must be one JSON object",
                    }
                else:
                    response = self._response(request)
                try:
                    connection.sendall((_canonical_json(response) + "\n").encode())
                except OSError:
                    pass

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.socket_path.unlink(missing_ok=True)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _new_nonce() -> str:
    return secrets.token_hex(16)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("ARTIFACT_INVALID", f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError("ARTIFACT_INVALID", f"{path} must contain one JSON object")
    return value


def _parse_seeds(spec: str) -> tuple[int, ...]:
    try:
        if ".." in spec:
            start_text, end_text = spec.split("..", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError
            return tuple(range(start, end + 1))
        values = tuple(int(item) for item in spec.split(","))
    except (TypeError, ValueError) as exc:
        raise ControllerError("SEED_DOMAIN", f"invalid seed range {spec!r}") from exc
    if not values:
        raise ControllerError("SEED_DOMAIN", "seed range cannot be empty")
    return values


def _seed_hashes() -> dict[str, str]:
    return {
        "development": _sha256_text(DEVELOPMENT_SEEDS),
        "regression": _sha256_text(REGRESSION_SEEDS),
        "heldout": _sha256_text(HELDOUT_SEEDS),
        "bundle": _sha256_text(
            _canonical_json(
                {
                    "development": DEVELOPMENT_SEEDS,
                    "regression": REGRESSION_SEEDS,
                    "heldout": HELDOUT_SEEDS,
                }
            )
        ),
    }


def _session_prompt(session: dict, budgets: dict[str, int | float] | None = None) -> str:
    selected = budgets or session["budget_ceiling"]
    candidate = Path(session["candidate"])
    if candidate.parts and candidate.parts[0] == session.get("worktree"):
        candidate = Path(*candidate.parts[1:])
    return (
        f"{_GOAL}\n\n"
        f"Assigned condition: {session['condition']}.\n"
        f"Assigned candidate in your worktree: {candidate.as_posix()}.\n"
        f"{_INTERFACE[session['condition']]}\n\n"
        f"Development seeds: {DEVELOPMENT_SEEDS}. Fixed regression seeds: "
        f"{REGRESSION_SEEDS}. Held-out episodes are withheld and are run only by the "
        f"external scorer after you stop.\n"
        f"External ceilings: {int(selected['tokens'])} new tokens, "
        f"{int(selected['episodes'])} development episodes, "
        f"{float(selected['wall_h']):g} wall hours. The controller is the sole "
        "authority for all three ceilings.\n"
        "Run every development or regression attempt only through:\n"
        'python "$AISLE_ABLATION_CONTROLLER" attempt '
        '--session "$AISLE_ABLATION_SESSION" --seeds <dev-seeds>\n'
        "Never invoke harness rollout, dora, Genesis, or the simulator directly.\n"
        "Do not access other sessions, accept operator hints, run a simulator outside "
        "the protected adapter, or change the assigned condition."
    )


def _artifact_hash(path: Path) -> str:
    return sha256_file(path) if path.exists() and path.is_file() else _sha256_text("")


def _session_dir(reference: str) -> Path:
    direct = Path(reference).expanduser()
    choices = (direct, Path.cwd() / direct, Path.cwd() / "sessions" / direct)
    for choice in choices:
        resolved = choice.resolve()
        if (resolved / "session.json").is_file():
            return resolved
    raise ControllerError("SESSION_UNKNOWN", f"cannot find session {reference!r}")


def _prepare(args: argparse.Namespace, runtime: ControllerRuntime, repo_root: Path) -> dict:
    if args.pairs <= 0:
        raise ControllerError("ARGUMENT", "--pairs must be a positive integer")
    out = args.out.expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise ControllerError("OUTPUT_EXISTS", f"campaign directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    pin = runtime.resolve_pin(repo_root, args.pin)
    assignments = paired_assignments(args.assignment_seed, args.pairs)
    runner_hash = sha256_file(Path(__file__))
    sessions: list[str] = []

    for index, condition in enumerate(assignments, start=1):
        session_id = f"S{index:04d}"
        sessions.append(session_id)
        session_dir = out / session_id
        worktree = session_dir / "worktree"
        session_dir.mkdir()
        runtime.create_worktree(pin, worktree)
        starter = STARTERS[condition]
        starter_path = worktree / starter
        if not starter_path.is_file():
            raise ControllerError("STARTER_MISSING", f"missing starter at pin: {starter}")
        candidate = CANDIDATES[condition]
        candidate_path = worktree / candidate
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(starter_path.read_bytes())

        record: dict[str, Any] = {
            "schema_version": 1,
            "session_id": session_id,
            "pair": (index - 1) // 2 + 1,
            "pair_slot": (index - 1) % 2,
            "assignment_seed": args.assignment_seed,
            "condition": condition,
            "state": "prepared",
            "pin": pin,
            "worktree": "worktree",
            "starter": starter.as_posix(),
            "candidate": (Path("worktree") / candidate).as_posix(),
            "budget_ceiling": {
                "tokens": MAX_NEW_TOKENS,
                "episodes": MAX_DEVELOPMENT_EPISODES,
                "wall_h": MAX_WALL_HOURS,
            },
            "provenance": {
                "runner_sha256": runner_hash,
                "prompt_sha256": "",
                "starter_sha256": sha256_file(starter_path),
                "candidate_initial_sha256": sha256_file(candidate_path),
                "seed_sha256": _seed_hashes(),
            },
            "operator_events": [],
            "artifact_sha256": {},
        }
        record["provenance"]["prompt_sha256"] = _sha256_text(_session_prompt(record))
        _write_json(session_dir / "session.json", record)
        (session_dir / "attempts.jsonl").write_text("", encoding="utf-8")
        (session_dir / "agent.jsonl").write_text("", encoding="utf-8")
        (session_dir / "token_samples.jsonl").write_text("", encoding="utf-8")
        _write_json(session_dir / "holdout.json", {"ok": False, "state": "pending"})
        _write_json(session_dir / "audit.json", {"ok": False, "state": "pending"})

    return {
        "ok": True,
        "pin": pin,
        "sessions": sessions,
        "assignments": assignments,
        "campaign_dir": str(out),
    }


def _strict_int(usage: dict, field: str) -> int:
    if field not in usage:
        raise TelemetryError(f"{field} is required")
    value = usage[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TelemetryError(f"{field} must be a non-negative integer")
    return value


def _agent_event(line: str, agent: str) -> tuple[dict, bool]:
    decoder = json.JSONDecoder()
    try:
        event, end = decoder.raw_decode(line.lstrip())
    except json.JSONDecodeError as exc:
        raise TelemetryError("agent stream line is not JSON") from exc
    if line.lstrip()[end:].strip() or not isinstance(event, dict):
        raise TelemetryError("agent stream line must be exactly one JSON object")
    if not isinstance(event.get("type"), str):
        raise TelemetryError("agent stream event has no string type")

    usage_event = False
    if agent == "claude" and event["type"] == "assistant":
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            raise TelemetryError("claude assistant event has no usage object")
        usage = message["usage"]
        for field in ("input_tokens", "cache_creation_input_tokens", "output_tokens"):
            _strict_int(usage, field)
        usage_event = True
    elif agent == "codex" and event["type"] == "turn.completed":
        usage = event.get("usage")
        if not isinstance(usage, dict):
            raise TelemetryError("codex turn.completed event has no usage object")
        input_tokens = _strict_int(usage, "input_tokens")
        cached_tokens = _strict_int(usage, "cached_input_tokens")
        _strict_int(usage, "output_tokens")
        if cached_tokens > input_tokens:
            raise TelemetryError("codex cached_input_tokens exceeds input_tokens")
        usage_event = True
    return event, usage_event


def _agent_version(agent: str) -> str:
    try:
        result = subprocess.run(
            [agent, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _has_unowned_simulator_descendant(
    agent_pid: int,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Detect simulator launchers below the agent; broker launches are controller children."""
    parent_by_pid: dict[int, int] = {}
    for status_path in proc_root.glob("[0-9]*/status"):
        try:
            pairs = (line.split(maxsplit=1) for line in status_path.read_text().splitlines())
            fields = {pair[0].rstrip(":"): pair[1].strip() for pair in pairs if len(pair) == 2}
            parent_by_pid[int(status_path.parent.name)] = int(fields["PPid"])
        except (KeyError, OSError, ValueError):
            continue

    descendants = {agent_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parent_by_pid.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    descendants.discard(agent_pid)

    for pid in descendants:
        try:
            argv = [
                item.decode(errors="replace")
                for item in (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except OSError:
            continue
        if not argv:
            continue
        executable = Path(argv[0]).name.lower()
        lowered = [argument.lower() for argument in argv[1:]]
        module_launch = any(
            lowered[index] == "-m"
            and lowered[index + 1] in {"dora", "genesis", "aisle.harness.rollout"}
            for index in range(len(lowered) - 1)
        )
        if (
            executable in {"dora", "genesis"}
            or (executable == "harness" and "rollout" in lowered)
            or module_launch
            or "aisle.harness.rollout" in lowered
            or any(
                marker in argument
                for argument in lowered
                for marker in (
                    "aisle/harness/rollout.py",
                    "script_rollout.py",
                )
            )
        ):
            return True
    return False


def _execute_agent(
    command: list[str],
    cwd: Path,
    agent: str,
    on_line: Callable[[str, float], None],
    stop_reason: Callable[[float], str | None],
    wall_ceiling_s: float,
    session_environment: dict[str, str],
) -> ExecutionResult:
    """Capture a live vendor stream while independently enforcing ceilings."""
    started = time.monotonic()
    environment = os.environ.copy()
    worktree_pythonpath = f"{cwd / 'src'}:{cwd}"
    if environment.get("PYTHONPATH"):
        worktree_pythonpath += f":{environment['PYTHONPATH']}"
    environment["PYTHONPATH"] = worktree_pythonpath
    environment.update(session_environment)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise ControllerError("AGENT_LAUNCH", str(exc)) from exc
    assert process.stdout is not None
    events: queue.Queue[str | None] = queue.Queue()

    def read_stream() -> None:
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    stopped = "agent_done"
    stream_done = False
    while not stream_done:
        elapsed = time.monotonic() - started
        reason = stop_reason(elapsed)
        if elapsed >= wall_ceiling_s:
            reason = "wall_budget"
        if _has_unowned_simulator_descendant(process.pid):
            reason = "unowned_launch"
        if reason:
            stopped = reason
            _kill_process_group(process)
        try:
            line = events.get(timeout=0.1)
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                stream_done = True
            continue
        if line is None:
            stream_done = True
            continue
        try:
            on_line(line, elapsed)
        except TelemetryError:
            stopped = "telemetry_invalid"
            _kill_process_group(process)
    remaining = max(0.0, wall_ceiling_s - (time.monotonic() - started))
    try:
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        stopped = "wall_budget"
        _kill_process_group(process)
        returncode = process.wait(timeout=30)
    reader.join(timeout=10)
    process.stdout.close()
    return ExecutionResult(
        stopped=stopped,
        returncode=returncode,
        wall_s=round(time.monotonic() - started, 3),
        agent_version=_agent_version(agent),
    )


@contextmanager
def _simulator_authority():
    GLOBAL_SIMULATOR_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with GLOBAL_SIMULATOR_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError(
                "CONCURRENT_SIMULATOR", "another ablation session owns simulator authority"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_run_budgets(tokens: int, episodes: int, wall_h: float) -> dict[str, int | float]:
    values = {
        "tokens": tokens,
        "episodes": episodes,
        "wall_h": wall_h,
    }
    if (
        isinstance(tokens, bool)
        or tokens <= 0
        or tokens > MAX_NEW_TOKENS
        or isinstance(episodes, bool)
        or episodes <= 0
        or episodes > MAX_DEVELOPMENT_EPISODES
        or not math.isfinite(wall_h)
        or wall_h <= 0
        or wall_h > MAX_WALL_HOURS
    ):
        raise ControllerError(
            "BUDGET_LIMIT",
            "ceilings must be positive and no greater than "
            f"{MAX_NEW_TOKENS} tokens, {MAX_DEVELOPMENT_EPISODES} episodes, "
            f"{MAX_WALL_HOURS:g} wall hours",
        )
    return values


def _initialize_episode_authority(worktree: Path, episodes: int) -> str | None:
    ledger_path = worktree / "runs" / "campaign_ledger.jsonl"
    if ledger_path.exists() and ledger_path.read_text(encoding="utf-8").strip():
        raise ControllerError("BUDGET_LEDGER_NOT_FRESH", "worktree already has campaign spend")
    campaign_budget = load_campaign_budget(worktree)
    underlying = int(campaign_budget["episodes"])
    if underlying < MAX_DEVELOPMENT_EPISODES:
        raise ControllerError("BUDGET_CONFIG", "frozen harness episode budget is unexpectedly low")
    reserve = underlying - episodes
    if reserve == 0:
        return None
    result = reserve_budget(worktree, CONTROLLER_RESERVATION, reserve)
    if not result.get("ok"):
        raise ControllerError("BUDGET_LEDGER", str(result.get("detail") or "reserve failed"))
    return str(result["entry"])


def _run(args: argparse.Namespace, runtime: ControllerRuntime) -> dict:
    session_dir = _session_dir(args.session)
    record = _load_json(session_dir / "session.json")
    if args.condition != record.get("condition"):
        raise ControllerError(
            "CONDITION_IMMUTABLE",
            f"session is assigned {record.get('condition')!r}, not {args.condition!r}",
        )
    if record.get("state") != "prepared":
        raise ControllerError("SESSION_STARTED", "session has already started")
    budgets = _validate_run_budgets(args.tokens, args.episodes, args.wall_h)
    worktree = session_dir / record["worktree"]
    if runtime.worktree_head(worktree) != record["pin"]:
        raise ControllerError("PIN_DRIFT", "session worktree is not at its prepared pin")
    prompt = _session_prompt(record, budgets)
    if _sha256_text(_session_prompt(record)) != record["provenance"]["prompt_sha256"]:
        raise ControllerError("PROMPT_DRIFT", "prepared prompt provenance does not verify")
    if sha256_file(Path(__file__)) != record["provenance"]["runner_sha256"]:
        raise ControllerError("RUNNER_DRIFT", "controller changed after preparation")
    starter_path = worktree / record["starter"]
    candidate_path = session_dir / record["candidate"]
    if (
        not starter_path.is_file()
        or sha256_file(starter_path) != record["provenance"]["starter_sha256"]
        or not candidate_path.is_file()
        or sha256_file(candidate_path) != record["provenance"]["candidate_initial_sha256"]
    ):
        raise ControllerError("STARTER_DRIFT", "starter or initial candidate changed before start")

    reservation_hash = _initialize_episode_authority(worktree, args.episodes)
    counter = UsageCounter(args.agent)
    usage_events = 0
    ledger_head: str | None = append_ledger(
        session_dir / "agent.jsonl",
        {
            "kind": "session_start",
            "agent": args.agent,
            "model": args.model,
            "prompt_sha256": _sha256_text(prompt),
        },
    )

    def on_line(line: str, wall_s: float) -> None:
        nonlocal ledger_head, usage_events
        try:
            event, has_usage = _agent_event(line, args.agent)
        except TelemetryError:
            ledger_head = append_ledger(
                session_dir / "agent.jsonl",
                {"kind": "telemetry_error", "line_sha256": _sha256_text(line)},
            )
            raise
        counter.feed(line)
        if has_usage:
            usage_events += 1
        ledger_head = append_ledger(
            session_dir / "agent.jsonl", {"kind": "agent_event", "record": event}
        )
        _append_jsonl(
            session_dir / "token_samples.jsonl",
            {"tokens": counter.total, "wall_s": round(wall_s, 3)},
        )

    def stop_reason(wall_s: float) -> str | None:
        if counter.total >= args.tokens:
            return "token_budget"
        if wall_s >= args.wall_h * 3600.0:
            return "wall_budget"
        return None

    attempt_broker = AttemptBroker(session_dir, runtime)
    record.update(
        {
            "state": "running",
            "agent": args.agent,
            "model": args.model,
            "budgets": budgets,
            "run_prompt_sha256": _sha256_text(prompt),
            "started_at_epoch": runtime.epoch_time(),
            "episode_reservation_sha256": reservation_hash,
            "attempt_broker": {
                "capability_sha256": attempt_broker.capability_sha256,
                "controller_sha256": sha256_file(Path(__file__)),
            },
        }
    )
    _write_json(session_dir / "session.json", record)
    executor = runtime.execute_agent or _execute_agent
    command = agent_cmd_campaign(args.agent, args.model, prompt)
    try:
        with _simulator_authority(), attempt_broker:
            execution = executor(
                command,
                worktree,
                args.agent,
                on_line,
                stop_reason,
                args.wall_h * 3600.0,
                attempt_broker.environment(),
            )
    except ControllerError:
        record.update(
            {
                "state": "agent_stopped",
                "ended_at_epoch": runtime.epoch_time(),
                "stop_reason": "infrastructure",
                "tokens_spent": None,
                "telemetry": {"valid": False, "ledger_head": ledger_head},
            }
        )
        _write_json(session_dir / "session.json", record)
        raise

    telemetry_valid = execution.stopped != "telemetry_invalid" and usage_events > 0
    if not telemetry_valid and execution.stopped != "telemetry_invalid":
        ledger_head = append_ledger(
            session_dir / "agent.jsonl",
            {"kind": "telemetry_error", "detail": "no usage-bearing events"},
        )
    ledger_head = append_ledger(
        session_dir / "agent.jsonl",
        {
            "kind": "session_stop",
            "reason": execution.stopped,
            "returncode": execution.returncode,
        },
    )
    frozen_drift = runtime.audit_frozen(worktree, record["pin"])
    budget_exceeded = execution.stopped in {"token_budget", "wall_budget"}
    record.update(
        {
            "state": "budget_exceeded" if budget_exceeded else "agent_stopped",
            "ended_at_epoch": runtime.epoch_time(),
            "stop_reason": execution.stopped,
            "agent_version": execution.agent_version,
            "returncode": execution.returncode,
            "wall_s": execution.wall_s,
            "tokens_spent": counter.total if telemetry_valid else None,
            "telemetry": {
                "valid": telemetry_valid,
                "usage_events": usage_events,
                "ledger_head": ledger_head,
            },
            "post_run_frozen_drift": frozen_drift,
        }
    )
    record["artifact_sha256"].update(
        {
            name: _artifact_hash(session_dir / name)
            for name in ("attempts.jsonl", "agent.jsonl", "token_samples.jsonl")
        }
    )
    _write_json(session_dir / "session.json", record)

    if not telemetry_valid:
        return {
            "ok": False,
            "code": "TELEMETRY_INVALID",
            "session": record["session_id"],
            "stopped": execution.stopped,
        }
    if execution.stopped == "token_budget":
        return {
            "ok": False,
            "code": "TOKEN_BUDGET_EXCEEDED",
            "session": record["session_id"],
            "tokens_spent": counter.total,
        }
    if execution.stopped == "wall_budget":
        return {
            "ok": False,
            "code": "WALL_BUDGET_EXCEEDED",
            "session": record["session_id"],
            "wall_s": execution.wall_s,
        }
    if execution.stopped == "unowned_launch":
        record["observed_unowned_launch"] = True
        _write_json(session_dir / "session.json", record)
        return {
            "ok": False,
            "code": "UNOWNED_LAUNCH",
            "session": record["session_id"],
        }
    if frozen_drift:
        return {
            "ok": False,
            "code": "FROZEN_DRIFT",
            "session": record["session_id"],
            "frozen_drift": frozen_drift,
        }
    if execution.stopped == "agent_done" and execution.returncode != 0:
        return {
            "ok": False,
            "code": "AGENT_EXIT",
            "session": record["session_id"],
            "returncode": execution.returncode,
        }
    return {
        "ok": True,
        "session": record["session_id"],
        "stopped": execution.stopped,
        "tokens_spent": counter.total,
        "wall_s": execution.wall_s,
    }


def _release_episode_reservation(worktree: Path, record: dict) -> str | None:
    if not record.get("episode_reservation_sha256"):
        return None
    prior = record.get("episode_reservation_release_sha256")
    if prior:
        return str(prior)
    if not verify_budget_ledger(worktree):
        raise ControllerError("BUDGET_LEDGER_INVALID", "development budget ledger was modified")
    return settle_budget(worktree, CONTROLLER_RESERVATION, episodes=0, wall_s=0.0)


def _attempt_ledger_state(session_dir: Path) -> tuple[dict[str, dict], dict[str, dict], int]:
    ledger_path = session_dir / "agent.jsonl"
    valid, _ = verify_ledger(ledger_path)
    entries = _read_jsonl(ledger_path)
    if not valid or entries is None:
        raise ControllerError("AGENT_LEDGER_INVALID", "agent ledger does not verify")

    admissions: dict[str, dict] = {}
    settlements: dict[str, dict] = {}
    for entry in entries:
        event = entry.get("event")
        if not isinstance(event, dict):
            raise ControllerError("AGENT_LEDGER_INVALID", "agent ledger event is invalid")
        kind = event.get("kind")
        if kind not in {"dev_attempt_admit", "dev_attempt_settle"}:
            continue
        attempt_id = event.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ControllerError("AGENT_LEDGER_INVALID", "attempt ledger ID is invalid")
        destination = admissions if kind == "dev_attempt_admit" else settlements
        if attempt_id in destination:
            raise ControllerError("AGENT_LEDGER_INVALID", "attempt ledger ID is duplicated")
        destination[attempt_id] = {"record": entry, "event": event}

    if not set(settlements) <= set(admissions):
        raise ControllerError("AGENT_LEDGER_INVALID", "attempt settlement lacks admission")
    charged = 0
    for attempt_id, admission in admissions.items():
        event = admission["event"]
        requested = event.get("requested_episodes")
        seeds = event.get("seeds")
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested <= 0
            or not isinstance(seeds, list)
            or requested != len(seeds)
        ):
            raise ControllerError("AGENT_LEDGER_INVALID", "attempt admission is invalid")
        settlement = settlements.get(attempt_id)
        if settlement is None:
            charged += requested
            continue
        actual = settlement["event"].get("actual_episodes")
        if (
            isinstance(actual, bool)
            or not isinstance(actual, int)
            or actual < 0
            or actual > requested
        ):
            raise ControllerError("AGENT_LEDGER_INVALID", "attempt settlement is invalid")
        charged += actual
    return admissions, settlements, charged


def _validate_development_result(
    attempt: AttemptResult,
    *,
    attempt_id: str,
    candidate_hash: str,
    requested_seeds: tuple[int, ...],
) -> AttemptResult:
    try:
        canonical = AttemptResult.from_dict(attempt.to_dict())
    except (AttributeError, ValueError) as exc:
        raise ControllerError("INFRA_PROTOCOL", "adapter returned an invalid attempt") from exc
    episode_seeds = tuple(episode.get("seed") for episode in canonical.episodes)
    if (
        canonical.attempt_id != attempt_id
        or canonical.candidate_hash != candidate_hash
        or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in episode_seeds)
        or episode_seeds != requested_seeds[: len(episode_seeds)]
    ):
        raise ControllerError(
            "INFRA_PROTOCOL", "adapter result does not match its controller admission"
        )
    return canonical


def _attempt(args: argparse.Namespace, runtime: ControllerRuntime) -> dict:
    session_dir = _session_dir(args.session)
    record = _load_json(session_dir / "session.json")
    if record.get("state") != "running":
        raise ControllerError("SESSION_NOT_RUNNING", "development attempts require a running agent")
    requested_seeds = _parse_seeds(args.seeds)
    if len(set(requested_seeds)) != len(requested_seeds):
        raise ControllerError("SEED_DOMAIN", "development attempt seeds must be unique")
    if not set(requested_seeds) <= set(_parse_seeds(DEVELOPMENT_SEEDS)):
        raise ControllerError("SEED_DOMAIN", "attempt seeds must stay in the development domain")

    budget = (record.get("budgets") or {}).get("episodes")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ControllerError("ARTIFACT_INVALID", "session development budget is invalid")
    candidate = session_dir / str(record.get("candidate", ""))
    if not candidate.is_file():
        raise ControllerError("CANDIDATE_MISSING", "assigned candidate is missing")
    candidate_hash = sha256_file(candidate)
    worktree = session_dir / str(record.get("worktree", ""))
    attempts_path = session_dir / "attempts.jsonl"

    with attempts_path.open("a+", encoding="utf-8") as attempts_file:
        fcntl.flock(attempts_file.fileno(), fcntl.LOCK_EX)
        try:
            _, _, charged = _attempt_ledger_state(session_dir)
            if charged + len(requested_seeds) > budget:
                raise ControllerError(
                    "EPISODE_BUDGET", "development attempt exceeds remaining episode budget"
                )

            nonce = _new_nonce()
            attempt_id = f"dev-{record['session_id']}-{nonce}"
            run_dir = worktree / "runs" / attempt_id
            if run_dir.exists():
                raise ControllerError("ATTEMPT_RUN_COLLISION", "attempt run ID already exists")
            admission_event = {
                "kind": "dev_attempt_admit",
                "attempt_id": attempt_id,
                "nonce": nonce,
                "condition": record["condition"],
                "candidate_sha256": candidate_hash,
                "seeds": list(requested_seeds),
                "requested_episodes": len(requested_seeds),
                "admitted_at_epoch": runtime.epoch_time(),
                "broker_capability_sha256": (record.get("attempt_broker") or {}).get(
                    "capability_sha256"
                ),
            }
            admission_hash = append_ledger(session_dir / "agent.jsonl", admission_event)

            adapter = runtime.adapter_factory(record["condition"], worktree)
            raw_attempt = adapter.rollout(candidate, args.seeds, attempt_id)
            attempt = _validate_development_result(
                raw_attempt,
                attempt_id=attempt_id,
                candidate_hash=candidate_hash,
                requested_seeds=requested_seeds,
            )
            attempt_raw = attempt.to_dict()
            attempt_line = _canonical_json(attempt_raw)
            attempt_hash = _sha256_text(attempt_line)
            attempts_file.seek(0, os.SEEK_END)
            attempts_file.write(attempt_line + "\n")
            attempts_file.flush()
            os.fsync(attempts_file.fileno())

            run_dir.mkdir(parents=True, exist_ok=True)
            adapter_manifest = run_dir / "manifest.json"
            adapter_manifest_hash = (
                sha256_file(adapter_manifest) if adapter_manifest.is_file() else None
            )
            controller_manifest = run_dir / "controller_attempt.json"
            _write_json(
                controller_manifest,
                {
                    "schema_version": 1,
                    "session_id": record["session_id"],
                    "condition": record["condition"],
                    "attempt_id": attempt_id,
                    "nonce": nonce,
                    "admission_sha256": admission_hash,
                    "candidate_sha256": candidate_hash,
                    "requested_seeds": list(requested_seeds),
                    "actual_episodes": len(attempt.episodes),
                    "attempt_sha256": attempt_hash,
                    "adapter_manifest_sha256": adapter_manifest_hash,
                },
            )
            controller_manifest_hash = sha256_file(controller_manifest)
            append_ledger(
                session_dir / "agent.jsonl",
                {
                    "kind": "dev_attempt_settle",
                    "attempt_id": attempt_id,
                    "admission_sha256": admission_hash,
                    "requested_episodes": len(requested_seeds),
                    "actual_episodes": len(attempt.episodes),
                    "attempt_sha256": attempt_hash,
                    "controller_manifest_sha256": controller_manifest_hash,
                    "adapter_manifest_sha256": adapter_manifest_hash,
                    "settled_at_epoch": runtime.epoch_time(),
                },
            )
        finally:
            fcntl.flock(attempts_file.fileno(), fcntl.LOCK_UN)

    return {
        "ok": True,
        "session": record["session_id"],
        "attempt_id": attempt_id,
        "episodes_used": len(attempt.episodes),
        "episodes_left": budget - charged - len(attempt.episodes),
        "attempt": attempt_raw,
    }


def _attempt_client(
    args: argparse.Namespace,
    environment: dict[str, str] | None = None,
) -> dict:
    env = os.environ if environment is None else environment
    socket_path = env.get("AISLE_ABLATION_SOCKET")
    capability = env.get("AISLE_ABLATION_CAPABILITY")
    if not socket_path or not capability:
        raise ControllerError(
            "ATTEMPT_BROKER_REQUIRED",
            "development attempts require the active controller broker",
        )
    request = {
        "capability": capability,
        "session": args.session,
        "seeds": args.seeds,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(MAX_WALL_HOURS * 3600.0)
            client.connect(socket_path)
            client.sendall((_canonical_json(request) + "\n").encode())
            response_line = client.makefile("r", encoding="utf-8").readline(1024 * 1024)
        response = json.loads(response_line)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("ATTEMPT_BROKER_UNAVAILABLE", str(exc)) from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise ControllerError("ATTEMPT_BROKER_PROTOCOL", "broker response is invalid")
    return response


def _heldout_result(attempt: AttemptResult) -> tuple[dict, str | None]:
    raw = attempt.to_dict()
    if attempt.failures.get("INFRA_SAFETY_UNAVAILABLE"):
        return (
            {
                "ok": False,
                "code": "INFRA_SAFETY_UNAVAILABLE",
                "outcome": "infrastructure",
                "pass1": None,
                "attempt": raw,
            },
            "INFRA_SAFETY_UNAVAILABLE",
        )
    infrastructure = sorted(
        code
        for code in attempt.failures
        if code.startswith("INFRA_") and code not in {"INFRA_ARGUMENT"}
    )
    if infrastructure:
        return (
            {
                "ok": False,
                "code": infrastructure[0],
                "outcome": "infrastructure",
                "pass1": None,
                "attempt": raw,
            },
            infrastructure[0],
        )
    episodes = list(attempt.episodes)
    expected_seeds = list(_parse_seeds(HELDOUT_SEEDS))
    if (
        len(episodes) != HELDOUT_EPISODES
        or [item.get("seed") for item in episodes] != expected_seeds
    ):
        return (
            {
                "ok": False,
                "code": "HOLDOUT_INCOMPLETE",
                "outcome": "infrastructure",
                "pass1": None,
                "attempt": raw,
            },
            "HOLDOUT_INCOMPLETE",
        )
    successes = sum(
        1
        for episode in episodes
        if episode.get("success") is True or episode.get("status") == "success"
    )
    return (
        {
            "ok": True,
            "outcome": "scored",
            "pass1": successes / HELDOUT_EPISODES,
            "episodes": episodes,
            "failures": dict(attempt.failures),
            "safety": attempt.safety.to_dict(),
            "attempt": raw,
        },
        None,
    )


def _validate_holdout_attempt(
    attempt: AttemptResult,
    *,
    run_id: str,
    candidate_hash: str,
) -> AttemptResult:
    try:
        canonical = AttemptResult.from_dict(attempt.to_dict())
    except (AttributeError, ValueError) as exc:
        raise ControllerError("INFRA_PROTOCOL", "held-out adapter result is invalid") from exc
    if canonical.attempt_id != run_id or canonical.candidate_hash != candidate_hash:
        raise ControllerError(
            "INFRA_PROTOCOL", "held-out adapter result does not match its admission"
        )
    return canonical


def _score_locked(
    args: argparse.Namespace,
    runtime: ControllerRuntime,
    session_dir: Path,
) -> dict:
    record = _load_json(session_dir / "session.json")
    if record.get("state") == "scoring_started":
        raise ControllerError(
            "SCORING_TERMINAL", "held-out scoring was already admitted and is terminal"
        )
    if record.get("state") == "scored":
        raise ControllerError("ALREADY_SCORED", "held-out scoring is single-use")
    if record.get("state") not in {"agent_stopped", "budget_exceeded"}:
        raise ControllerError("AGENT_NOT_STOPPED", "held-out scoring requires a stopped agent")
    if args.holdout != HELDOUT_SEEDS:
        requested = set(_parse_seeds(args.holdout))
        development = set(_parse_seeds(DEVELOPMENT_SEEDS))
        reason = "overlaps development seeds" if requested & development else "is not frozen"
        raise ControllerError("SEED_DOMAIN", f"held-out range {reason}")
    existing = _load_json(session_dir / "holdout.json")
    if existing.get("state") in {"complete", "scoring_started"}:
        raise ControllerError("ALREADY_SCORED", "held-out scoring is single-use")

    worktree = session_dir / record["worktree"]
    release_hash = _release_episode_reservation(worktree, record)
    if release_hash:
        record["episode_reservation_release_sha256"] = release_hash
    candidate = session_dir / record["candidate"]
    candidate_hash = sha256_file(candidate) if candidate.is_file() else None
    nonce = _new_nonce()
    run_id = f"holdout-{record['session_id']}-{nonce}"
    admission = {
        "nonce": nonce,
        "run_id": run_id,
        "started_at_epoch": runtime.epoch_time(),
        "state": "scoring_started",
        "candidate_sha256": candidate_hash,
        "controller_manifest_sha256": None,
        "adapter_manifest_sha256": None,
    }
    record["state"] = "scoring_started"
    record["scoring_admission"] = admission
    _write_json(session_dir / "session.json", record)
    _write_json(
        session_dir / "holdout.json",
        {
            "ok": False,
            "state": "scoring_started",
            "session": record["session_id"],
            "nonce": nonce,
            "run_id": run_id,
            "started_at_epoch": admission["started_at_epoch"],
        },
    )

    run_dir = worktree / "runs" / run_id
    if run_dir.exists():
        raise ControllerError(
            "HOLDOUT_RUN_COLLISION", "held-out run ID existed before scoring admission"
        )
    if not candidate.is_file():
        result = {
            "ok": True,
            "state": "complete",
            "outcome": "no_deliverable",
            "pass1": 0.0,
            "episodes": [],
            "failures": {},
            "safety": None,
        }
        error_code = None
    else:
        adapter = runtime.adapter_factory(record["condition"], worktree)
        attempt = _validate_holdout_attempt(
            adapter.rollout(candidate, HELDOUT_SEEDS, run_id),
            run_id=run_id,
            candidate_hash=str(candidate_hash),
        )
        result, error_code = _heldout_result(attempt)
        result["state"] = "complete"

        run_dir.mkdir(parents=True, exist_ok=True)
        attempt_sha256 = _sha256_text(_canonical_json(attempt.to_dict()))
        adapter_manifest = run_dir / "manifest.json"
        adapter_manifest_hash = (
            sha256_file(adapter_manifest) if adapter_manifest.is_file() else None
        )
        controller_manifest = run_dir / "controller_holdout.json"
        _write_json(
            controller_manifest,
            {
                "schema_version": 1,
                "session_id": record["session_id"],
                "nonce": nonce,
                "run_id": run_id,
                "candidate_sha256": candidate_hash,
                "attempt_sha256": attempt_sha256,
                "adapter_manifest_sha256": adapter_manifest_hash,
            },
        )
        admission.update(
            {
                "state": "complete",
                "attempt_sha256": attempt_sha256,
                "controller_manifest_sha256": sha256_file(controller_manifest),
                "adapter_manifest_sha256": adapter_manifest_hash,
            }
        )
    result.update({"nonce": nonce, "run_id": run_id})
    _write_json(session_dir / "holdout.json", result)
    record["state"] = "scored"
    admission["state"] = "complete"
    admission["completed_at_epoch"] = runtime.epoch_time()
    record["artifact_sha256"]["holdout.json"] = sha256_file(session_dir / "holdout.json")
    _write_json(session_dir / "session.json", record)
    if error_code:
        return {
            "ok": False,
            "code": error_code,
            "session": record["session_id"],
            "pass1": None,
        }
    return {
        "ok": True,
        "session": record["session_id"],
        "outcome": result["outcome"],
        "pass1": result["pass1"],
    }


def _score(args: argparse.Namespace, runtime: ControllerRuntime) -> dict:
    session_dir = _session_dir(args.session)
    with (session_dir / "attempts.jsonl").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _score_locked(args, runtime, session_dir)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_jsonl(path: Path) -> list[dict] | None:
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                return None
            records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return records


def _budget_authority_valid(worktree: Path, record: dict, entries: list[dict]) -> bool:
    reservation_hash = record.get("episode_reservation_sha256")
    if reservation_hash is None:
        return not any(entry.get("run_id") == CONTROLLER_RESERVATION for entry in entries)
    controller_entries = [
        entry for entry in entries if entry.get("run_id") == CONTROLLER_RESERVATION
    ]
    expected_reserve = int(load_campaign_budget(worktree)["episodes"]) - int(
        record["budgets"]["episodes"]
    )
    reserves = [entry for entry in controller_entries if entry.get("kind") == "reserve"]
    settlements = [entry for entry in controller_entries if entry.get("kind") == "settle"]
    if (
        len(reserves) != 1
        or reserves[0].get("hash") != reservation_hash
        or reserves[0].get("episodes") != expected_reserve
    ):
        return False
    if record.get("state") in {"scored", "scoring_started"}:
        release_hash = record.get("episode_reservation_release_sha256")
        return (
            len(settlements) == 1
            and settlements[0].get("hash") == release_hash
            and settlements[0].get("episodes") == 0
            and settlements[0].get("wall_s") == 0.0
        )
    return not settlements and record.get("episode_reservation_release_sha256") is None


def _attempt_correlation(
    worktree: Path,
    record: dict,
    attempts: list[dict],
    agent_entries: list[dict],
) -> tuple[set[str], set[str], int]:
    issues: set[str] = set()
    admissions: dict[str, list[dict]] = {}
    settlements: dict[str, list[dict]] = {}
    for entry in agent_entries:
        event = entry.get("event")
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind not in {"dev_attempt_admit", "dev_attempt_settle"}:
            continue
        attempt_id = event.get("attempt_id")
        if not isinstance(attempt_id, str):
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        destination = admissions if kind == "dev_attempt_admit" else settlements
        destination.setdefault(attempt_id, []).append(entry)

    attempt_records: dict[str, tuple[dict, AttemptResult]] = {}
    for raw in attempts:
        try:
            attempt = AttemptResult.from_dict(raw)
        except ValueError:
            issues.add("ATTEMPTS_INVALID")
            continue
        if attempt.attempt_id in attempt_records:
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        attempt_records[attempt.attempt_id] = (raw, attempt)

    correlated: set[str] = set()
    charged_episodes = 0
    for attempt_id, admission_records in admissions.items():
        if len(admission_records) != 1:
            continue
        requested_episodes = admission_records[0]["event"].get("requested_episodes")
        if (
            isinstance(requested_episodes, bool)
            or not isinstance(requested_episodes, int)
            or requested_episodes <= 0
        ):
            continue
        settlement_records = settlements.get(attempt_id, [])
        if len(settlement_records) != 1:
            charged_episodes += requested_episodes
            continue
        actual_episodes = settlement_records[0]["event"].get("actual_episodes")
        if (
            isinstance(actual_episodes, bool)
            or not isinstance(actual_episodes, int)
            or actual_episodes < 0
            or actual_episodes > requested_episodes
        ):
            charged_episodes += requested_episodes
            continue
        charged_episodes += actual_episodes

    all_ids = set(admissions) | set(settlements) | set(attempt_records)
    for attempt_id in all_ids:
        admission_records = admissions.get(attempt_id, [])
        settlement_records = settlements.get(attempt_id, [])
        attempt_record = attempt_records.get(attempt_id)
        if len(admission_records) != 1 or len(settlement_records) != 1 or attempt_record is None:
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue

        admission_entry = admission_records[0]
        admission = admission_entry["event"]
        settlement = settlement_records[0]["event"]
        raw, attempt = attempt_record
        requested = admission.get("seeds")
        requested_is_valid = (
            isinstance(requested, list)
            and bool(requested)
            and all(not isinstance(seed, bool) and isinstance(seed, int) for seed in requested)
        )
        if (
            not requested_is_valid
            or len(requested) != admission.get("requested_episodes")
            or len(set(requested)) != len(requested)
            or not set(requested) <= set(_parse_seeds(DEVELOPMENT_SEEDS))
        ):
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        episode_seeds = [episode.get("seed") for episode in attempt.episodes]
        attempt_hash = _sha256_text(_canonical_json(raw))
        controller_manifest = worktree / "runs" / attempt_id / "controller_attempt.json"
        if not controller_manifest.is_file():
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        try:
            manifest = _load_json(controller_manifest)
        except ControllerError:
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        adapter_manifest = worktree / "runs" / attempt_id / "manifest.json"
        adapter_manifest_hash = (
            sha256_file(adapter_manifest) if adapter_manifest.is_file() else None
        )
        actual = len(attempt.episodes)
        if (
            any(isinstance(seed, bool) or not isinstance(seed, int) for seed in episode_seeds)
            or episode_seeds != requested[:actual]
            or isinstance(settlement.get("actual_episodes"), bool)
            or settlement.get("actual_episodes") != actual
            or isinstance(settlement.get("requested_episodes"), bool)
            or settlement.get("requested_episodes") != len(requested)
            or settlement.get("attempt_sha256") != attempt_hash
            or settlement.get("admission_sha256") != admission_entry.get("sha256")
            or settlement.get("controller_manifest_sha256") != sha256_file(controller_manifest)
            or settlement.get("adapter_manifest_sha256") != adapter_manifest_hash
            or admission.get("candidate_sha256") != attempt.candidate_hash
            or admission.get("broker_capability_sha256")
            != (record.get("attempt_broker") or {}).get("capability_sha256")
            or manifest.get("attempt_id") != attempt_id
            or manifest.get("session_id") != record.get("session_id")
            or manifest.get("condition") != record.get("condition")
            or manifest.get("admission_sha256") != admission_entry.get("sha256")
            or manifest.get("candidate_sha256") != attempt.candidate_hash
            or manifest.get("requested_seeds") != requested
            or manifest.get("actual_episodes") != actual
            or manifest.get("attempt_sha256") != attempt_hash
            or manifest.get("adapter_manifest_sha256") != adapter_manifest_hash
        ):
            issues.add("ATTEMPT_CORRELATION_INVALID")
            continue
        correlated.add(attempt_id)

    episode_budget = (record.get("budgets") or {}).get("episodes", 0)
    if (
        isinstance(episode_budget, bool)
        or not isinstance(episode_budget, int)
        or charged_episodes > episode_budget
    ):
        issues.add("DEVELOPMENT_BUDGET_EXCEEDED")
    return issues, correlated, charged_episodes


def _authorized_holdout(worktree: Path, record: dict) -> str | None:
    admission = record.get("scoring_admission")
    if (
        record.get("state") != "scored"
        or not isinstance(admission, dict)
        or admission.get("state") != "complete"
        or not isinstance(admission.get("run_id"), str)
        or not admission.get("controller_manifest_sha256")
    ):
        return None
    run_id = admission["run_id"]
    controller_manifest = worktree / "runs" / run_id / "controller_holdout.json"
    if not controller_manifest.is_file() or sha256_file(controller_manifest) != admission.get(
        "controller_manifest_sha256"
    ):
        return None
    try:
        manifest = _load_json(controller_manifest)
    except ControllerError:
        return None
    adapter_manifest = worktree / "runs" / run_id / "manifest.json"
    adapter_manifest_hash = sha256_file(adapter_manifest) if adapter_manifest.is_file() else None
    if (
        manifest.get("session_id") != record.get("session_id")
        or manifest.get("run_id") != run_id
        or manifest.get("nonce") != admission.get("nonce")
        or manifest.get("candidate_sha256") != admission.get("candidate_sha256")
        or manifest.get("adapter_manifest_sha256") != adapter_manifest_hash
        or admission.get("adapter_manifest_sha256") != adapter_manifest_hash
    ):
        return None
    return run_id


def _rollout_contamination(
    worktree: Path,
    record: dict,
    development_run_ids: set[str],
) -> tuple[set[str], set[str]]:
    issues: set[str] = set()
    exclusions: set[str] = set()
    heldout = set(_parse_seeds(HELDOUT_SEEDS))
    external_run_id = _authorized_holdout(worktree, record)
    runs_dir = worktree / "runs"
    run_dirs = sorted(path for path in runs_dir.glob("*") if path.is_dir())
    for run_dir in run_dirs:
        run_id = run_dir.name
        owned = run_id in development_run_ids or run_id == external_run_id
        if not owned:
            issues.add("UNOWNED_LAUNCH")
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            issues.add("ROLLOUT_ARTIFACT_INVALID")
            continue
        if not isinstance(manifest, dict):
            issues.add("ROLLOUT_ARTIFACT_INVALID")
            continue
        if manifest.get("run_id") != run_id:
            issues.add("ROLLOUT_ARTIFACT_INVALID")
        if manifest.get("env_baseline") != "origin/main":
            issues.add("UNTRUSTED_ROLLOUT")
            exclusions.add("UNTRUSTED_ROLLOUT")
        episodes_path = run_dir / "episodes.jsonl"
        episodes = _read_jsonl(episodes_path) if episodes_path.exists() else []
        if episodes is None:
            issues.add("ROLLOUT_ARTIFACT_INVALID")
        elif run_id != external_run_id and any(
            episode.get("seed") in heldout for episode in episodes
        ):
            issues.add("HELDOUT_EXPOSURE")
            exclusions.add("HELDOUT_EXPOSURE")
    return issues, exclusions


def _audit_session(
    session_dir: Path,
    runtime: ControllerRuntime,
    overlaps: set[str],
) -> dict:
    record = _load_json(session_dir / "session.json")
    worktree = session_dir / record["worktree"]
    issues: list[str] = []
    exclusions: list[str] = []
    agent_entries = _read_jsonl(session_dir / "agent.jsonl")
    if agent_entries is None:
        agent_entries = []

    expected_assignment = paired_assignments(record.get("assignment_seed"), record.get("pair", 0))
    assignment_index = (record.get("pair", 0) - 1) * 2 + record.get("pair_slot", -1)
    if (
        assignment_index < 0
        or assignment_index >= len(expected_assignment)
        or expected_assignment[assignment_index] != record.get("condition")
    ):
        issues.append("ASSIGNMENT_DRIFT")
    provenance = record.get("provenance") or {}
    if provenance.get("runner_sha256") != sha256_file(Path(__file__)):
        issues.append("RUNNER_DRIFT")
    try:
        expected_prompt = _sha256_text(_session_prompt(record))
    except (KeyError, TypeError):
        expected_prompt = None
    if provenance.get("prompt_sha256") != expected_prompt:
        issues.append("PROMPT_DRIFT")
    broker = record.get("attempt_broker")
    if record.get("state") != "prepared" and (
        not isinstance(broker, dict)
        or broker.get("controller_sha256") != provenance.get("runner_sha256")
        or not isinstance(broker.get("capability_sha256"), str)
        or len(broker["capability_sha256"]) != 64
    ):
        issues.append("ATTEMPT_BROKER_INVALID")
    if record.get("state") != "prepared":
        try:
            run_prompt_hash = _sha256_text(_session_prompt(record, record["budgets"]))
        except (KeyError, TypeError, ValueError):
            run_prompt_hash = None
        start_prompt_hash = None
        if agent_entries:
            start_event = agent_entries[0].get("event")
            if isinstance(start_event, dict) and start_event.get("kind") == "session_start":
                start_prompt_hash = start_event.get("prompt_sha256")
        if (
            record.get("run_prompt_sha256") != run_prompt_hash
            or start_prompt_hash != run_prompt_hash
        ):
            issues.append("PROMPT_DRIFT")
    starter = worktree / str(record.get("starter", ""))
    if not starter.is_file() or provenance.get("starter_sha256") != sha256_file(starter):
        issues.append("STARTER_DRIFT")
    if provenance.get("seed_sha256") != _seed_hashes():
        issues.append("SEED_DRIFT")
    if runtime.worktree_head(worktree) != record.get("pin"):
        issues.append("PIN_DRIFT")
        exclusions.append("WRONG_STARTING_COMMIT")

    frozen = runtime.audit_frozen(worktree, record.get("pin", ""))
    if frozen:
        issues.append("FROZEN_DRIFT")
        exclusions.append("FROZEN_DRIFT")
    status = runtime.worktree_status(worktree)
    if status is None:
        issues.append("WORKTREE_AUDIT_UNAVAILABLE")

    ledger_ok, ledger_head = verify_ledger(session_dir / "agent.jsonl")
    expected_head = (record.get("telemetry") or {}).get("ledger_head")
    if not ledger_ok or (expected_head is not None and ledger_head != expected_head):
        issues.append("AGENT_LEDGER_INVALID")
    samples = _read_jsonl(session_dir / "token_samples.jsonl")
    if samples is None:
        issues.append("TOKEN_TELEMETRY_INVALID")
        exclusions.append("MISSING_TOKEN_TELEMETRY")
    else:
        prior_tokens = -1
        prior_wall = -1.0
        for sample in samples:
            tokens = sample.get("tokens")
            wall_s = sample.get("wall_s")
            if (
                isinstance(tokens, bool)
                or not isinstance(tokens, int)
                or tokens < prior_tokens
                or isinstance(wall_s, bool)
                or not isinstance(wall_s, (int, float))
                or wall_s < prior_wall
            ):
                issues.append("TOKEN_TELEMETRY_INVALID")
                exclusions.append("MISSING_TOKEN_TELEMETRY")
                break
            prior_tokens, prior_wall = tokens, float(wall_s)
        telemetry = record.get("telemetry") or {}
        if record.get("state") != "prepared":
            if not telemetry.get("valid") or not samples:
                issues.append("TOKEN_TELEMETRY_MISSING")
                exclusions.append("MISSING_TOKEN_TELEMETRY")
            elif samples[-1].get("tokens") != record.get("tokens_spent"):
                issues.append("TOKEN_TOTAL_MISMATCH")
                exclusions.append("MISSING_TOKEN_TELEMETRY")

    for name in ("attempts.jsonl", "agent.jsonl", "token_samples.jsonl", "holdout.json"):
        expected = (record.get("artifact_sha256") or {}).get(name)
        if expected is not None and expected != _artifact_hash(session_dir / name):
            issues.append(f"ARTIFACT_HASH_MISMATCH:{name}")
    attempts = _read_jsonl(session_dir / "attempts.jsonl")
    development_run_ids: set[str] = set()
    development_episodes: int | None = None
    if attempts is None:
        issues.append("ATTEMPTS_INVALID")
    else:
        for raw in attempts:
            try:
                attempt = AttemptResult.from_dict(raw)
            except ValueError:
                issues.append("ATTEMPTS_INVALID")
                break
            if attempt.failures.get("INFRA_SAFETY_UNAVAILABLE"):
                issues.append("SAFETY_TELEMETRY_MISSING")
                exclusions.append("MISSING_SAFETY_TELEMETRY")
            if attempt.safety.ungated:
                issues.append("UNGATED_SAFETY")
        (
            correlation_issues,
            development_run_ids,
            development_episodes,
        ) = _attempt_correlation(
            worktree,
            record,
            attempts,
            agent_entries,
        )
        issues.extend(correlation_issues)

    if record.get("operator_events"):
        issues.append("OPERATOR_INTERVENTION")
        exclusions.append("OPERATOR_INTERVENTION")
    if record.get("observed_unowned_launch"):
        issues.append("UNOWNED_LAUNCH")
        exclusions.append("UNOWNED_LAUNCH")
    if record.get("session_id") in overlaps:
        issues.append("CONCURRENT_SIMULATOR")
        exclusions.append("CONCURRENT_SIMULATOR")
    rollout_issues, rollout_exclusions = _rollout_contamination(
        worktree, record, development_run_ids
    )
    issues.extend(rollout_issues)
    exclusions.extend(rollout_exclusions)
    if not verify_budget_ledger(worktree):
        issues.append("BUDGET_LEDGER_INVALID")
    else:
        entries = budget_ledger(worktree)
        reservation = record.get("episode_reservation_sha256")
        if reservation and not any(entry.get("hash") == reservation for entry in entries):
            issues.append("BUDGET_RESERVATION_MISSING")
        if not _budget_authority_valid(worktree, record, entries):
            issues.append("BUDGET_AUTHORITY_TAMPER")

    issues = sorted(set(issues))
    exclusions = sorted(set(exclusions))
    result = {
        "ok": not issues,
        "session": record.get("session_id"),
        "issues": issues,
        "exclusions": exclusions,
        "frozen_drift": frozen,
        "dirty_tree": status,
        "agent_ledger_head": ledger_head,
        "development_episodes": development_episodes,
    }
    _write_json(session_dir / "audit.json", result)
    return result


def _campaign_sessions(directory: Path) -> list[Path]:
    directory = directory.expanduser().resolve()
    if (directory / "session.json").is_file():
        return [directory]
    sessions = sorted(
        path for path in directory.iterdir() if path.is_dir() and (path / "session.json").is_file()
    )
    if not sessions:
        raise ControllerError("SESSION_UNKNOWN", f"no sessions under {directory}")
    return sessions


def _overlapping_sessions(session_dirs: list[Path]) -> set[str]:
    intervals: list[tuple[str, float, float]] = []
    for session_dir in session_dirs:
        record = _load_json(session_dir / "session.json")
        start, end = record.get("started_at_epoch"), record.get("ended_at_epoch")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
            intervals.append((str(record.get("session_id")), float(start), float(end)))
    overlaps: set[str] = set()
    for index, (left_id, left_start, left_end) in enumerate(intervals):
        for right_id, right_start, right_end in intervals[index + 1 :]:
            if max(left_start, right_start) < min(left_end, right_end):
                overlaps.update((left_id, right_id))
    return overlaps


def _audit(args: argparse.Namespace, runtime: ControllerRuntime) -> dict:
    sessions = _campaign_sessions(args.dir)
    overlaps = _overlapping_sessions(sessions)
    results = [_audit_session(session, runtime, overlaps) for session in sessions]
    return {
        "ok": all(result["ok"] for result in results),
        "campaign_dir": str(args.dir.expanduser().resolve()),
        "sessions": results,
    }


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--pin", required=True)
    prepare.add_argument("--pairs", type=int, required=True)
    prepare.add_argument("--assignment-seed", type=int, required=True)
    prepare.add_argument("--out", type=Path, required=True)

    run = commands.add_parser("run")
    run.add_argument("--session", required=True)
    run.add_argument("--condition", choices=("aisle", "script"), required=True)
    run.add_argument("--agent", choices=("claude", "codex"), required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--tokens", type=int, required=True)
    run.add_argument("--episodes", type=int, required=True)
    run.add_argument("--wall-h", type=float, required=True)

    score = commands.add_parser("score")
    score.add_argument("--session", required=True)
    score.add_argument("--holdout", required=True)

    audit = commands.add_parser("audit")
    audit.add_argument("--dir", type=Path, required=True)

    attempt = commands.add_parser("attempt")
    attempt.add_argument("--session", required=True)
    attempt.add_argument("--seeds", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime: ControllerRuntime | None = None,
    repo_root: Path = REPO_ROOT,
) -> int:
    runtime_injected = runtime is not None
    runtime = runtime or ControllerRuntime()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument in {"-h", "--help"} for argument in arguments):
        public_commands = {"prepare", "run", "score", "audit"}
        command = next(
            (argument for argument in arguments if argument in public_commands),
            None,
        )
        result = {
            "ok": True,
            "usage": {
                "command": command,
                "commands": ["prepare", "run", "score", "audit"],
            },
        }
        print(_canonical_json(result))
        return 0
    try:
        args = _parser().parse_args(arguments)
        if args.command == "prepare":
            result = _prepare(args, runtime, repo_root)
        elif args.command == "run":
            result = _run(args, runtime)
        elif args.command == "score":
            result = _score(args, runtime)
        elif args.command == "attempt":
            result = _attempt(args, runtime) if runtime_injected else _attempt_client(args)
        else:
            result = _audit(args, runtime)
    except ControllerError as exc:
        result = {"ok": False, "code": exc.code, "error": exc.detail}
    except Exception as exc:  # noqa: BLE001 - preserve CON-8 for unexpected infrastructure
        print(f"[s1-ablation] unexpected controller error: {exc!r}", file=sys.stderr)
        result = {"ok": False, "code": "INTERNAL", "error": str(exc)}
    print(_canonical_json(result))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
