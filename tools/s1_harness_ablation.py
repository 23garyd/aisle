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
import hmac
import json
import math
import os
import queue
import secrets
import shutil
import signal
import socket
import stat
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

from build_s1_agent_env import (  # noqa: E402
    AgentEnvironmentSpec,
    inspect_agent_environment,
)
from campaign import UsageCounter, agent_cmd_campaign, audit_frozen  # noqa: E402

from aisle.harness import ablation_adapters as ablation_adapters_module  # noqa: E402
from aisle.harness import native_sandbox  # noqa: E402
from aisle.harness import rollout as rollout_module  # noqa: E402
from aisle.harness import script_rollout as script_rollout_module  # noqa: E402
from aisle.harness.ablation import (  # noqa: E402
    AttemptResult,
    append_ledger,
    paired_assignments,
    sha256_file,
    verify_ledger,
)
from aisle.harness.ablation_adapters import AisleAdapter, ScriptAdapter  # noqa: E402
from aisle.harness.native_sandbox import SandboxPolicy  # noqa: E402
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
ATTEMPT_CLIENT = TOOLS_DIR / "s1_harness_attempt_client.py"
CONTROLLER_RESERVATION = "__s1_ablation_controller_reserve__"
GLOBAL_SIMULATOR_LOCK = Path("/tmp/aisle-s1-harness-ablation-simulator.lock")
_ATTEMPT_PROTOCOL = 1
_MAX_BROKER_MESSAGE_BYTES = 1024 * 1024
_PROBE_TIMEOUT_S = 30.0

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
    process_group_clean: bool = True


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


def _controller_project_root() -> Path:
    if REPO_ROOT.parent.name == ".worktrees":
        return REPO_ROOT.parent.parent
    return REPO_ROOT


def _default_agent_environment(agent: str) -> Path:
    del agent
    return _controller_project_root() / ".s1-agent-environments" / "s1"


def _default_agent_executable(agent: str) -> Path:
    executable = shutil.which(agent)
    if executable is None:
        raise ControllerError("AGENT_EXECUTABLE", f"cannot resolve {agent} executable")
    return Path(executable)


def _default_execute_sandbox_probe(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> dict:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError("SANDBOX_PROBE", "sandbox probe could not complete") from exc
    try:
        decoder = json.JSONDecoder()
        result, end = decoder.raw_decode(completed.stdout.lstrip())
    except json.JSONDecodeError as exc:
        raise ControllerError("SANDBOX_PROBE", "sandbox probe output is not JSON") from exc
    if (
        completed.stdout.lstrip()[end:].strip()
        or completed.returncode != 0
        or not isinstance(result, dict)
    ):
        raise ControllerError("SANDBOX_PROBE", "sandbox probe response is invalid")
    return result


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
    worktrees_root: Path | None = None
    agent_environment: Callable[[str], Path] = _default_agent_environment
    inspect_agent_environment: Callable[[Path], AgentEnvironmentSpec] = inspect_agent_environment
    agent_executable: Callable[[str], Path] = _default_agent_executable
    bwrap_executable: Callable[[], Path] = lambda: native_sandbox.BWRAP
    build_bwrap_argv: Callable[[SandboxPolicy, list[str], dict[str, str]], list[str]] = (
        native_sandbox.build_bwrap_argv
    )
    sandbox_probe_command: Callable[[], list[str]] = native_sandbox.sandbox_probe_command
    execute_sandbox_probe: Callable[[list[str], Path, dict[str, str], float], dict] = (
        _default_execute_sandbox_probe
    )


@dataclass(frozen=True)
class BrokerEndpoint:
    """Controller-owned files and identity evidence for one broker instance."""

    runtime_dir: Path
    socket_path: Path
    credential_path: Path
    broker_id: str
    socket_inode: int
    socket_ctime_ns: int
    socket_identity_sha256: str
    credential_sha256: str
    started_at_epoch: float
    expires_at_epoch: float


@dataclass(frozen=True)
class AuthorizedAttempt:
    """A request whose identity, candidate bytes, nonce, and seeds were rechecked."""

    session_id: str
    condition: str
    nonce: int
    candidate: Path
    candidate_relpath: str
    candidate_sha256: str
    seeds: tuple[int, ...]
    seed_domain: str
    request_sha256: str


def _private_runtime_directory(path: Path, worktree: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ControllerError("ATTEMPT_BROKER_START", "broker runtime cannot be a symlink")
    try:
        requested.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ControllerError("ATTEMPT_BROKER_START", "cannot create broker runtime") from exc
    try:
        resolved = requested.resolve(strict=True)
        info = requested.lstat()
    except OSError as exc:
        raise ControllerError("ATTEMPT_BROKER_START", "cannot inspect broker runtime") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or resolved.is_relative_to(worktree)
        or worktree.is_relative_to(resolved)
    ):
        raise ControllerError(
            "ATTEMPT_BROKER_START",
            "broker runtime must be private, controller-owned, and outside the worktree",
        )
    return resolved


def _candidate_relative(record: dict, worktree: Path) -> str:
    candidate = Path(str(record.get("candidate", "")))
    if not candidate.is_absolute():
        candidate = Path(str(record.get("worktree", ""))) / candidate
        if not candidate.is_absolute():
            candidate = worktree.parent / candidate
    try:
        return candidate.resolve(strict=False).relative_to(worktree).as_posix()
    except (OSError, ValueError) as exc:
        raise ControllerError(
            "ARTIFACT_INVALID", "assigned candidate is outside its worktree"
        ) from exc


class AttemptBroker:
    """Authenticated controller-owned IPC for in-session trusted launches."""

    _REQUEST_KEYS = frozenset(
        {
            "protocol",
            "session_id",
            "condition",
            "nonce",
            "credential",
            "candidate_relpath",
            "candidate_sha256",
            "seeds",
        }
    )

    def __init__(
        self,
        session_dir: Path,
        runtime: ControllerRuntime,
        *,
        runtime_dir: Path | None = None,
        controller_hashes: dict[str, str] | None = None,
        expires_at_epoch: float | None = None,
    ) -> None:
        self.session_dir = session_dir.resolve()
        self.runtime = runtime
        record = _load_json(self.session_dir / "session.json")
        self.session_id = str(record.get("session_id"))
        self.condition = str(record.get("condition"))
        self.worktree = Path(str(record.get("worktree", ""))).resolve()
        self.candidate_relpath = _candidate_relative(record, self.worktree)
        self.runtime_dir = (
            runtime_dir or Path("/tmp") / f"aisle-s1-{secrets.token_hex(8)}"
        ).resolve(strict=False)
        self.controller_hashes = dict(
            controller_hashes
            or (record.get("isolation") if isinstance(record.get("isolation"), dict) else {})
        )
        if not self.controller_hashes:
            self.controller_hashes = {"runner_sha256": sha256_file(Path(__file__))}
        if not all(
            isinstance(key, str) and isinstance(value, str) and len(value) == 64
            for key, value in self.controller_hashes.items()
        ):
            raise ControllerError("ATTEMPT_BROKER_START", "controller hash binding is invalid")
        self.started_at_epoch = float(runtime.epoch_time())
        requested_expiry = (
            self.started_at_epoch + MAX_WALL_HOURS * 3600.0
            if expires_at_epoch is None
            else float(expires_at_epoch)
        )
        if (
            not math.isfinite(self.started_at_epoch)
            or not math.isfinite(requested_expiry)
            or requested_expiry <= self.started_at_epoch
            or requested_expiry - self.started_at_epoch > MAX_WALL_HOURS * 3600.0
        ):
            raise ControllerError("ATTEMPT_BROKER_START", "credential expiry is invalid")
        self.expires_at_epoch = requested_expiry
        self.broker_id = secrets.token_hex(16)
        self.__root_capability = secrets.token_bytes(32)
        self._credential: str | None = None
        self._endpoint: BrokerEndpoint | None = None
        self._last_nonce = 0
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_reason: str | None = None

    @property
    def endpoint(self) -> BrokerEndpoint:
        if self._endpoint is None:
            raise ControllerError("ATTEMPT_BROKER_START", "broker has not started")
        return self._endpoint

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    @property
    def credential_sha256(self) -> str:
        return self.endpoint.credential_sha256

    def environment(self) -> dict[str, str]:
        """No host path or credential is inherited by the sandboxed process."""

        return {}

    def _credential_binding(
        self,
        *,
        socket_inode: int,
        socket_ctime_ns: int,
        socket_identity_sha256: str,
    ) -> dict:
        return {
            "protocol": _ATTEMPT_PROTOCOL,
            "broker_id": self.broker_id,
            "session_id": self.session_id,
            "condition": self.condition,
            "socket_inode": socket_inode,
            "socket_ctime_ns": socket_ctime_ns,
            "socket_identity_sha256": socket_identity_sha256,
            "controller_hashes": self.controller_hashes,
            "started_at_epoch": self.started_at_epoch,
            "expires_at_epoch": self.expires_at_epoch,
        }

    def start(self) -> BrokerEndpoint:
        if self._endpoint is not None:
            return self._endpoint
        runtime_dir = _private_runtime_directory(self.runtime_dir, self.worktree)
        socket_path = runtime_dir / "attempt.sock"
        credential_path = runtime_dir / "credential.json"
        if socket_path.exists() or credential_path.exists():
            raise ControllerError("ATTEMPT_BROKER_START", "broker runtime is not fresh")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            socket_stat = socket_path.lstat()
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise ControllerError("ATTEMPT_BROKER_START", "broker socket is invalid")
            socket_identity = {
                "inode": socket_stat.st_ino,
                "ctime_ns": socket_stat.st_ctime_ns,
                "mode": stat.S_IMODE(socket_stat.st_mode),
            }
            socket_identity_sha256 = _sha256_text(_canonical_json(socket_identity))
            binding = self._credential_binding(
                socket_inode=socket_stat.st_ino,
                socket_ctime_ns=socket_stat.st_ctime_ns,
                socket_identity_sha256=socket_identity_sha256,
            )
            credential = hmac.new(
                self.__root_capability,
                _canonical_json(binding).encode(),
                hashlib.sha256,
            ).hexdigest()
            config = {
                "protocol": _ATTEMPT_PROTOCOL,
                "session_id": self.session_id,
                "condition": self.condition,
                "credential": credential,
                "candidate_relpath": self.candidate_relpath,
                "expires_at_epoch": self.expires_at_epoch,
            }
            _write_json(credential_path, config)
            os.chmod(credential_path, 0o600)
            server.listen(1)
            server.settimeout(0.1)
        except Exception:
            server.close()
            socket_path.unlink(missing_ok=True)
            credential_path.unlink(missing_ok=True)
            raise
        self._credential = credential
        self._server = server
        self._endpoint = BrokerEndpoint(
            runtime_dir=runtime_dir,
            socket_path=socket_path,
            credential_path=credential_path,
            broker_id=self.broker_id,
            socket_inode=socket_stat.st_ino,
            socket_ctime_ns=socket_stat.st_ctime_ns,
            socket_identity_sha256=socket_identity_sha256,
            credential_sha256=_sha256_text(credential),
            started_at_epoch=self.started_at_epoch,
            expires_at_epoch=self.expires_at_epoch,
        )
        self._thread = threading.Thread(
            target=self._serve,
            name=f"aisle-attempt-broker-{self.session_id}",
            daemon=True,
        )
        self._thread.start()
        return self._endpoint

    def __enter__(self) -> AttemptBroker:
        self.start()
        return self

    def _socket_identity_valid(self) -> bool:
        try:
            info = self.endpoint.socket_path.lstat()
        except OSError:
            return False
        identity = {
            "inode": info.st_ino,
            "ctime_ns": info.st_ctime_ns,
            "mode": stat.S_IMODE(info.st_mode),
        }
        return (
            stat.S_ISSOCK(info.st_mode)
            and info.st_ino == self.endpoint.socket_inode
            and info.st_ctime_ns == self.endpoint.socket_ctime_ns
            and hmac.compare_digest(
                _sha256_text(_canonical_json(identity)),
                self.endpoint.socket_identity_sha256,
            )
        )

    def _authorize_unlocked(self, request: dict) -> AuthorizedAttempt:
        if not isinstance(request, dict) or set(request) != self._REQUEST_KEYS:
            raise ControllerError(
                "ATTEMPT_BROKER_AUTH",
                "attempt request was not authorized for this session",
            )
        nonce = request.get("nonce")
        if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= self._last_nonce:
            raise ControllerError(
                "ATTEMPT_BROKER_REPLAY", "attempt nonce is not strictly increasing"
            )
        if (
            request.get("protocol") != _ATTEMPT_PROTOCOL
            or isinstance(request.get("protocol"), bool)
            or request.get("session_id") != self.session_id
            or request.get("condition") != self.condition
            or not isinstance(request.get("credential"), str)
            or self._credential is None
            or not hmac.compare_digest(request["credential"], self._credential)
            or not self._socket_identity_valid()
        ):
            raise ControllerError(
                "ATTEMPT_BROKER_AUTH",
                "attempt request was not authorized for this session",
            )
        now = float(self.runtime.epoch_time())
        if not math.isfinite(now) or now < self.started_at_epoch or now >= self.expires_at_epoch:
            raise ControllerError("ATTEMPT_BROKER_EXPIRED", "attempt credential expired")
        relative = request.get("candidate_relpath")
        if not isinstance(relative, str) or relative != self.candidate_relpath:
            raise ControllerError(
                "CANDIDATE_PATH", "candidate path is not assigned to this session"
            )
        try:
            candidate = (self.worktree / relative).resolve(strict=True)
        except OSError as exc:
            raise ControllerError("CANDIDATE_MISSING", "assigned candidate is missing") from exc
        if not candidate.is_relative_to(self.worktree) or not candidate.is_file():
            raise ControllerError("CANDIDATE_PATH", "candidate escaped the assigned worktree")
        supplied_hash = request.get("candidate_sha256")
        candidate_hash = sha256_file(candidate)
        if (
            not isinstance(supplied_hash, str)
            or len(supplied_hash) != 64
            or not hmac.compare_digest(candidate_hash, supplied_hash)
        ):
            raise ControllerError("CANDIDATE_HASH", "candidate bytes changed before admission")
        seeds = _parse_attempt_seed_csv(request.get("seeds"))
        development = set(_parse_seeds(DEVELOPMENT_SEEDS))
        if not set(seeds) <= development:
            raise ControllerError(
                "SEED_DOMAIN", "attempt seeds must stay in the development domain"
            )
        self._last_nonce = nonce
        return AuthorizedAttempt(
            session_id=self.session_id,
            condition=self.condition,
            nonce=nonce,
            candidate=candidate,
            candidate_relpath=relative,
            candidate_sha256=candidate_hash,
            seeds=seeds,
            seed_domain=(
                "regression" if set(seeds) <= set(_parse_seeds(REGRESSION_SEEDS)) else "development"
            ),
            request_sha256=_sha256_text(_canonical_json(request)),
        )

    def authorize(self, request: dict) -> AuthorizedAttempt:
        self.start()
        lock_path = self.session_dir / "attempt.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._authorize_unlocked(request)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _response(self, request: object) -> dict:
        if not isinstance(request, dict):
            return {
                "ok": False,
                "code": "ATTEMPT_BROKER_PROTOCOL",
                "error": "attempt request must be one JSON object",
            }
        lock_path = self.session_dir / "attempt.lock"
        try:
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    authorized = self._authorize_unlocked(request)
                    return _execute_authorized_attempt(
                        self.session_dir,
                        authorized,
                        self.runtime,
                        credential_sha256=self.credential_sha256,
                    )
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except ControllerError as exc:
            if exc.code in {
                "ATTEMPT_BROKER_AUTH",
                "ATTEMPT_BROKER_EXPIRED",
                "ATTEMPT_BROKER_PROTOCOL",
                "ATTEMPT_BROKER_REPLAY",
                "CANDIDATE_HASH",
                "CANDIDATE_PATH",
                "SEED_DOMAIN",
            }:
                self._fatal_reason = "broker_policy"
            return {"ok": False, "code": exc.code, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001 - normalize the trusted server boundary
            self._fatal_reason = "broker_failure"
            print(f"[s1-ablation] attempt broker error: {exc!r}", file=sys.stderr)
            return {
                "ok": False,
                "code": "ATTEMPT_BROKER_FAILURE",
                "error": "trusted attempt execution failed",
            }

    @staticmethod
    def _decode_request(encoded: bytes) -> dict:
        if len(encoded) > _MAX_BROKER_MESSAGE_BYTES:
            raise ValueError("request too large")
        text = encoded.decode("utf-8")
        decoder = json.JSONDecoder()
        request, end = decoder.raw_decode(text.lstrip())
        if text.lstrip()[end:].strip() or not isinstance(request, dict):
            raise ValueError("request is not one object")
        return request

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
                received = bytearray()
                try:
                    while True:
                        chunk = connection.recv(
                            min(
                                65536,
                                _MAX_BROKER_MESSAGE_BYTES + 1 - len(received),
                            )
                        )
                        if not chunk:
                            break
                        received.extend(chunk)
                        if len(received) > _MAX_BROKER_MESSAGE_BYTES:
                            raise ValueError("request too large")
                    request = self._decode_request(bytes(received))
                    response = self._response(request)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self._fatal_reason = "broker_policy"
                    response = {
                        "ok": False,
                        "code": "ATTEMPT_BROKER_PROTOCOL",
                        "error": "attempt request must be one bounded JSON object",
                    }
                try:
                    connection.sendall((_canonical_json(response) + "\n").encode())
                except OSError:
                    pass

    def stop(self) -> bool:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        clean = self._thread is None or not self._thread.is_alive()
        if self._endpoint is not None:
            self._endpoint.socket_path.unlink(missing_ok=True)
            self._endpoint.credential_path.unlink(missing_ok=True)
        self._credential = None
        self.__root_capability = b""
        return clean

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _new_nonce() -> str:
    return secrets.token_hex(16)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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


def _parse_attempt_seed_csv(spec: object) -> tuple[int, ...]:
    if not isinstance(spec, str) or not spec:
        raise ControllerError("SEED_DOMAIN", "attempt seeds must be a non-empty CSV")
    pieces = spec.split(",")
    if any(not piece.isascii() or not piece.isdecimal() for piece in pieces):
        raise ControllerError("SEED_DOMAIN", "attempt seeds must be decimal CSV values")
    values = tuple(int(piece) for piece in pieces)
    if len(set(values)) != len(values):
        raise ControllerError("SEED_DOMAIN", "attempt seeds must be unique")
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
    worktree = Path(session["worktree"])
    try:
        candidate = candidate.relative_to(worktree)
    except ValueError:
        if candidate.parts and candidate.parts[0] == worktree.name:
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
        "python /opt/aisle/attempt-client --seeds <csv>\n"
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
    configured_root = (
        runtime.worktrees_root
        if runtime.worktrees_root is not None
        else (
            repo_root.parent if repo_root.parent.name == ".worktrees" else repo_root / ".worktrees"
        )
    )
    configured_root = configured_root.expanduser().resolve(strict=False)
    try:
        configured_root.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as exc:
        raise ControllerError(
            "WORKTREE_ROOT",
            "configured session worktree root is unavailable",
        ) from exc
    if not configured_root.is_dir():
        raise ControllerError("WORKTREE_ROOT", "configured session worktree root is invalid")

    for index, condition in enumerate(assignments, start=1):
        session_id = f"S{index:04d}"
        sessions.append(session_id)
        session_dir = out / session_id
        worktree_tag = _sha256_text(f"{out}:{session_id}")[:16]
        worktree = configured_root / f"s1-{worktree_tag}-{session_id}"
        session_dir.mkdir()
        if worktree.exists():
            raise ControllerError("WORKTREE_CREATE", f"session worktree already exists: {worktree}")
        runtime.create_worktree(pin, worktree)
        worktree = worktree.resolve(strict=True)
        if worktree.parent != configured_root:
            raise ControllerError(
                "WORKTREE_CREATE",
                "prepared session worktree escaped the configured direct-child root",
            )
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
            "worktrees_root": str(configured_root),
            "worktrees_root_sha256": _sha256_text(str(configured_root)),
            "worktree": str(worktree),
            "worktree_path_sha256": _sha256_text(str(worktree)),
            "starter": starter.as_posix(),
            "candidate": str(candidate_path.resolve()),
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
        (session_dir / "attempt.lock").write_text("", encoding="utf-8")
        (session_dir / "score.lock").write_text("", encoding="utf-8")
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
    environment.pop("PYTHONPATH", None)
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
            if process.poll() is None:
                stopped = "stdout_eof"
                _kill_process_group(process)
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


def _agent_environment_sha256(spec: AgentEnvironmentSpec) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "python": spec.python,
                "lock_sha256": spec.lock_sha256,
                "distributions": list(spec.distributions),
                "executables": list(spec.executables),
            }
        )
    )


def _trusted_run_components(
    record: dict,
    runtime: ControllerRuntime,
    worktree: Path,
    agent: str,
) -> tuple[dict[str, str], Path, Path, Path]:
    agent_env = runtime.agent_environment(agent).expanduser().resolve(strict=True)
    agent_spec = runtime.inspect_agent_environment(agent_env)
    agent_executable = runtime.agent_executable(agent).expanduser().resolve(strict=True)
    bwrap = runtime.bwrap_executable().expanduser().resolve(strict=True)
    if not agent_env.is_dir() or not agent_executable.is_file() or not bwrap.is_file():
        raise ControllerError("SANDBOX_COMPONENT", "sandbox launch component is unavailable")
    runner_module = rollout_module if record.get("condition") == "aisle" else script_rollout_module
    runner_file = Path(str(runner_module.__file__)).resolve(strict=True)
    adapter_file = Path(str(ablation_adapters_module.__file__)).resolve(strict=True)
    policy_file = Path(str(native_sandbox.__file__)).resolve(strict=True)
    starter = worktree / str(record.get("starter", ""))
    frozen_identity = {
        "pin": record.get("pin"),
        "starter_sha256": sha256_file(starter),
        "seed_sha256": record.get("provenance", {}).get("seed_sha256"),
    }
    hashes = {
        "broker_sha256": sha256_file(Path(__file__)),
        "client_sha256": sha256_file(ATTEMPT_CLIENT),
        "sandbox_policy_sha256": sha256_file(policy_file),
        "agent_environment_sha256": _agent_environment_sha256(agent_spec),
        "bwrap_sha256": sha256_file(bwrap),
        "adapter_sha256": sha256_file(adapter_file),
        "runner_sha256": sha256_file(runner_file),
        "frozen_source_sha256": _sha256_text(_canonical_json(frozen_identity)),
    }
    return hashes, agent_env, agent_executable, bwrap


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
        broker_reason = attempt_broker.fatal_reason
        if broker_reason is not None:
            return broker_reason
        if counter.total >= args.tokens:
            return "token_budget"
        if wall_s >= args.wall_h * 3600.0:
            return "wall_budget"
        return None

    component_hashes, agent_env, agent_executable, _ = _trusted_run_components(
        record,
        runtime,
        worktree,
        args.agent,
    )
    started_at_epoch = runtime.epoch_time()
    attempt_broker = AttemptBroker(
        session_dir,
        runtime,
        controller_hashes=component_hashes,
        expires_at_epoch=started_at_epoch + args.wall_h * 3600.0,
    )
    endpoint = attempt_broker.start()
    policy = SandboxPolicy(
        worktree=worktree,
        agent_env=agent_env,
        runtime_dir=endpoint.runtime_dir,
        attempt_client=ATTEMPT_CLIENT,
        agent_executable=agent_executable,
        credential_mounts=(),
    )
    probe_argv = runtime.build_bwrap_argv(policy, runtime.sandbox_probe_command(), {})
    probe_result = runtime.execute_sandbox_probe(
        probe_argv,
        worktree,
        os.environ.copy(),
        _PROBE_TIMEOUT_S,
    )
    probe_ok, probe_errors = native_sandbox.verify_sandbox_probe(probe_result)
    if not probe_ok:
        attempt_broker.stop()
        raise ControllerError(
            "SANDBOX_PROBE",
            f"sandbox probe did not match the required contract: {list(probe_errors)}",
        )
    vendor_command = agent_cmd_campaign(args.agent, args.model, prompt)
    vendor_command[0] = "/opt/aisle/agent"
    command = runtime.build_bwrap_argv(policy, vendor_command, os.environ.copy())
    isolation = {
        **component_hashes,
        "broker_id": endpoint.broker_id,
        "credential_sha256": endpoint.credential_sha256,
        "socket_inode": endpoint.socket_inode,
        "socket_ctime_ns": endpoint.socket_ctime_ns,
        "socket_identity_sha256": endpoint.socket_identity_sha256,
        "broker_started_at_epoch": endpoint.started_at_epoch,
        "credential_expires_at_epoch": endpoint.expires_at_epoch,
        "namespace_probe": probe_result,
        "namespace_probe_sha256": _sha256_text(_canonical_json(probe_result)),
        "probe_argv_sha256": _sha256_text(_canonical_json(probe_argv)),
        "sandbox_argv_sha256": _sha256_text(_canonical_json(command)),
    }
    record.update(
        {
            "state": "running",
            "agent": args.agent,
            "model": args.model,
            "budgets": budgets,
            "run_prompt_sha256": _sha256_text(prompt),
            "started_at_epoch": started_at_epoch,
            "episode_reservation_sha256": reservation_hash,
            "attempt_broker": {
                "credential_sha256": endpoint.credential_sha256,
                "broker_id": endpoint.broker_id,
                "socket_identity_sha256": endpoint.socket_identity_sha256,
                "controller_sha256": sha256_file(Path(__file__)),
            },
            "isolation": isolation,
        }
    )
    _write_json(session_dir / "session.json", record)
    executor = runtime.execute_agent or _execute_agent
    try:
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
        broker_clean = attempt_broker.stop()
        record.update(
            {
                "state": "agent_stopped",
                "ended_at_epoch": runtime.epoch_time(),
                "stop_reason": "infrastructure",
                "tokens_spent": None,
                "telemetry": {"valid": False, "ledger_head": ledger_head},
                "isolation_cleanup": {
                    "broker_stopped": broker_clean,
                    "credential_revoked": not endpoint.credential_path.exists(),
                    "socket_removed": not endpoint.socket_path.exists(),
                    "process_group_clean": False,
                },
            }
        )
        _write_json(session_dir / "session.json", record)
        raise
    broker_failure = attempt_broker.fatal_reason
    broker_clean = attempt_broker.stop()
    if broker_failure is not None and execution.stopped == "agent_done":
        execution = ExecutionResult(
            stopped=broker_failure,
            returncode=execution.returncode,
            wall_s=execution.wall_s,
            agent_version=execution.agent_version,
            process_group_clean=execution.process_group_clean,
        )

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
            "isolation_cleanup": {
                "broker_stopped": broker_clean,
                "credential_revoked": not endpoint.credential_path.exists(),
                "socket_removed": not endpoint.socket_path.exists(),
                "process_group_clean": execution.process_group_clean,
            },
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
    if execution.stopped in {"broker_failure", "broker_policy"}:
        return {
            "ok": False,
            "code": (
                "ATTEMPT_BROKER_FAILURE"
                if execution.stopped == "broker_failure"
                else "ATTEMPT_BROKER_POLICY"
            ),
            "session": record["session_id"],
        }
    if not broker_clean or not execution.process_group_clean:
        return {
            "ok": False,
            "code": "SANDBOX_CLEANUP",
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


def _execute_authorized_attempt(
    session_dir: Path,
    authorized: AuthorizedAttempt,
    runtime: ControllerRuntime,
    *,
    credential_sha256: str,
) -> dict:
    """Admit, launch, persist, and settle one already-authenticated locked request."""

    record = _load_json(session_dir / "session.json")
    if (
        record.get("state") != "running"
        or record.get("session_id") != authorized.session_id
        or record.get("condition") != authorized.condition
    ):
        raise ControllerError("SESSION_NOT_RUNNING", "development attempts require a running agent")
    budget = (record.get("budgets") or {}).get("episodes")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ControllerError("ARTIFACT_INVALID", "session development budget is invalid")
    worktree = Path(str(record.get("worktree", ""))).resolve()
    candidate = (worktree / authorized.candidate_relpath).resolve(strict=True)
    if (
        candidate != authorized.candidate
        or not candidate.is_relative_to(worktree)
        or not hmac.compare_digest(sha256_file(candidate), authorized.candidate_sha256)
    ):
        raise ControllerError("CANDIDATE_HASH", "candidate changed after authorization")
    attempts_path = session_dir / "attempts.jsonl"
    _, _, charged = _attempt_ledger_state(session_dir)
    if charged + len(authorized.seeds) > budget:
        raise ControllerError(
            "EPISODE_BUDGET", "development attempt exceeds remaining episode budget"
        )

    attempt_id = f"dev-{record['session_id']}-{authorized.nonce}"
    run_dir = worktree / "runs" / attempt_id
    if run_dir.exists():
        raise ControllerError("ATTEMPT_RUN_COLLISION", "attempt run ID already exists")
    admitted_at = runtime.epoch_time()
    admission_event = {
        "kind": "dev_attempt_admit",
        "attempt_id": attempt_id,
        "nonce": authorized.nonce,
        "request_sha256": authorized.request_sha256,
        "credential_sha256": credential_sha256,
        "condition": record["condition"],
        "candidate_relpath": authorized.candidate_relpath,
        "candidate_sha256": authorized.candidate_sha256,
        "seed_domain": authorized.seed_domain,
        "seeds": list(authorized.seeds),
        "requested_episodes": len(authorized.seeds),
        "admitted_at_epoch": admitted_at,
    }
    admission_hash = append_ledger(session_dir / "agent.jsonl", admission_event)

    adapter = runtime.adapter_factory(record["condition"], worktree)
    simulator_started_at = runtime.epoch_time()
    with _simulator_authority():
        raw_attempt = adapter.rollout(
            candidate,
            ",".join(str(seed) for seed in authorized.seeds),
            attempt_id,
        )
    simulator_ended_at = runtime.epoch_time()
    attempt = _validate_development_result(
        raw_attempt,
        attempt_id=attempt_id,
        candidate_hash=authorized.candidate_sha256,
        requested_seeds=authorized.seeds,
    )
    attempt_raw = attempt.to_dict()
    attempt_line = _canonical_json(attempt_raw)
    attempt_hash = _sha256_text(attempt_line)
    with attempts_path.open("a", encoding="utf-8") as attempts_file:
        attempts_file.write(attempt_line + "\n")
        attempts_file.flush()
        os.fsync(attempts_file.fileno())

    run_dir.mkdir(parents=True, exist_ok=True)
    adapter_manifest = run_dir / "manifest.json"
    adapter_manifest_hash = sha256_file(adapter_manifest) if adapter_manifest.is_file() else None
    controller_manifest = run_dir / "controller_attempt.json"
    _write_json(
        controller_manifest,
        {
            "schema_version": 1,
            "session_id": record["session_id"],
            "condition": record["condition"],
            "attempt_id": attempt_id,
            "nonce": authorized.nonce,
            "request_sha256": authorized.request_sha256,
            "credential_sha256": credential_sha256,
            "admission_sha256": admission_hash,
            "candidate_relpath": authorized.candidate_relpath,
            "candidate_sha256": authorized.candidate_sha256,
            "requested_seeds": list(authorized.seeds),
            "actual_episodes": len(attempt.episodes),
            "attempt_sha256": attempt_hash,
            "adapter_manifest_sha256": adapter_manifest_hash,
            "simulator_started_at_epoch": simulator_started_at,
            "simulator_ended_at_epoch": simulator_ended_at,
        },
    )
    controller_manifest_hash = sha256_file(controller_manifest)
    append_ledger(
        session_dir / "agent.jsonl",
        {
            "kind": "dev_attempt_settle",
            "attempt_id": attempt_id,
            "nonce": authorized.nonce,
            "request_sha256": authorized.request_sha256,
            "admission_sha256": admission_hash,
            "requested_episodes": len(authorized.seeds),
            "actual_episodes": len(attempt.episodes),
            "attempt_sha256": attempt_hash,
            "controller_manifest_sha256": controller_manifest_hash,
            "adapter_manifest_sha256": adapter_manifest_hash,
            "simulator_started_at_epoch": simulator_started_at,
            "simulator_ended_at_epoch": simulator_ended_at,
            "settled_at_epoch": runtime.epoch_time(),
        },
    )
    return {
        "ok": True,
        "attempt_id": attempt_id,
        "episodes_used": len(attempt.episodes),
        "episodes_left": budget - charged - len(attempt.episodes),
    }


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
    if record.get("state") in {"scoring_started", "scoring_invalid"}:
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
    if existing.get("state") in {"complete", "scoring_started", "invalid"}:
        raise ControllerError("ALREADY_SCORED", "held-out scoring is single-use")

    worktree = session_dir / record["worktree"]
    release_hash = _release_episode_reservation(worktree, record)
    if release_hash:
        record["episode_reservation_release_sha256"] = release_hash
    candidate = session_dir / record["candidate"]
    candidate_hash = sha256_file(candidate) if candidate.is_file() else None
    component_hashes, _, _, _ = _trusted_run_components(
        record,
        runtime,
        worktree,
        str(record.get("agent")),
    )
    nonce = _new_nonce()
    run_id = f"holdout-{record['session_id']}-{nonce}"
    admission = {
        "nonce": nonce,
        "scorer_nonce": nonce,
        "run_id": run_id,
        "started_at_epoch": runtime.epoch_time(),
        "state": "scoring_started",
        "candidate_sha256": candidate_hash,
        "heldout_seed_sha256": _seed_hashes()["heldout"],
        "controller_sha256": sha256_file(Path(__file__)),
        "adapter_sha256": component_hashes["adapter_sha256"],
        "runner_sha256": component_hashes["runner_sha256"],
        "frozen_source_sha256": component_hashes["frozen_source_sha256"],
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
    try:
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
            with _simulator_authority():
                raw_attempt = adapter.rollout(candidate, HELDOUT_SEEDS, run_id)
            attempt = _validate_holdout_attempt(
                raw_attempt,
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
                    "scorer_nonce": nonce,
                    "run_id": run_id,
                    "candidate_sha256": candidate_hash,
                    "heldout_seed_sha256": admission["heldout_seed_sha256"],
                    "controller_sha256": admission["controller_sha256"],
                    "adapter_sha256": admission["adapter_sha256"],
                    "runner_sha256": admission["runner_sha256"],
                    "frozen_source_sha256": admission["frozen_source_sha256"],
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
    except Exception as exc:
        admission["state"] = "invalid"
        admission["invalid_at_epoch"] = runtime.epoch_time()
        admission["failure_sha256"] = _sha256_text(type(exc).__name__)
        record["state"] = "scoring_invalid"
        _write_json(session_dir / "session.json", record)
        _write_json(
            session_dir / "holdout.json",
            {
                "ok": False,
                "state": "invalid",
                "outcome": "infrastructure",
                "session": record["session_id"],
                "nonce": nonce,
                "run_id": run_id,
                "failure_sha256": admission["failure_sha256"],
            },
        )
        raise
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
    with (session_dir / "score.lock").open("a+", encoding="utf-8") as lock:
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
    authenticated_nonces: list[int] = []
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
        if kind == "dev_attempt_admit":
            nonce = event.get("nonce")
            if isinstance(nonce, bool) or not isinstance(nonce, int):
                issues.add("ATTEMPT_BROKER_INVALID")
            else:
                authenticated_nonces.append(nonce)
    if any(
        current <= prior
        for prior, current in zip(authenticated_nonces, authenticated_nonces[1:], strict=False)
    ):
        issues.add("ATTEMPT_BROKER_INVALID")

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
            or admission.get("credential_sha256")
            != (record.get("attempt_broker") or {}).get("credential_sha256")
            or admission.get("nonce") != settlement.get("nonce")
            or admission.get("request_sha256") != settlement.get("request_sha256")
            or manifest.get("attempt_id") != attempt_id
            or manifest.get("session_id") != record.get("session_id")
            or manifest.get("condition") != record.get("condition")
            or manifest.get("admission_sha256") != admission_entry.get("sha256")
            or manifest.get("nonce") != admission.get("nonce")
            or manifest.get("request_sha256") != admission.get("request_sha256")
            or manifest.get("credential_sha256") != admission.get("credential_sha256")
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
        or manifest.get("scorer_nonce") != admission.get("scorer_nonce")
        or manifest.get("candidate_sha256") != admission.get("candidate_sha256")
        or manifest.get("heldout_seed_sha256") != _seed_hashes()["heldout"]
        or admission.get("heldout_seed_sha256") != _seed_hashes()["heldout"]
        or manifest.get("controller_sha256") != admission.get("controller_sha256")
        or manifest.get("adapter_sha256") != admission.get("adapter_sha256")
        or manifest.get("runner_sha256") != admission.get("runner_sha256")
        or manifest.get("frozen_source_sha256") != admission.get("frozen_source_sha256")
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
    worktree = Path(str(record["worktree"])).resolve(strict=False)
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
    configured_root = Path(str(record.get("worktrees_root", ""))).resolve(strict=False)
    if (
        worktree.parent != configured_root
        or record.get("worktrees_root_sha256") != _sha256_text(str(configured_root))
        or record.get("worktree_path_sha256") != _sha256_text(str(worktree))
    ):
        issues.append("WORKTREE_CONTAINMENT")
    broker = record.get("attempt_broker")
    isolation = record.get("isolation")
    if record.get("state") != "prepared" and (
        not isinstance(broker, dict)
        or broker.get("controller_sha256") != provenance.get("runner_sha256")
        or not isinstance(broker.get("credential_sha256"), str)
        or len(broker["credential_sha256"]) != 64
        or not isinstance(broker.get("broker_id"), str)
        or not broker.get("broker_id")
        or not isinstance(isolation, dict)
        or isolation.get("credential_sha256") != broker.get("credential_sha256")
        or isolation.get("broker_id") != broker.get("broker_id")
        or isolation.get("socket_identity_sha256") != broker.get("socket_identity_sha256")
    ):
        issues.append("ATTEMPT_BROKER_INVALID")
    if record.get("state") != "prepared" and isinstance(isolation, dict):
        try:
            current_hashes, _, _, _ = _trusted_run_components(
                record,
                runtime,
                worktree,
                str(record.get("agent")),
            )
        except (ControllerError, OSError, ValueError):
            current_hashes = {}
        if not current_hashes or any(
            isolation.get(key) != value for key, value in current_hashes.items()
        ):
            issues.append("ISOLATION_HASH_DRIFT")
        probe = isolation.get("namespace_probe")
        probe_ok, _ = native_sandbox.verify_sandbox_probe(probe)
        if not probe_ok or isolation.get("namespace_probe_sha256") != _sha256_text(
            _canonical_json(probe)
        ):
            issues.append("SANDBOX_PROBE_INVALID")
        cleanup = record.get("isolation_cleanup")
        if not isinstance(cleanup, dict) or cleanup != {
            "broker_stopped": True,
            "credential_revoked": True,
            "socket_removed": True,
            "process_group_clean": True,
        }:
            issues.append("SANDBOX_CLEANUP_INVALID")
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
    if record.get("state") in {"scoring_started", "scoring_invalid"}:
        issues.append("SCORING_TERMINAL_INVALID")
        exclusions.append("SCORING_TERMINAL_INVALID")
    if record.get("state") == "scored" and _authorized_holdout(worktree, record) is None:
        issues.append("SCORING_ATTRIBUTION_INVALID")
        exclusions.append("SCORING_ATTRIBUTION_INVALID")
    if record.get("observed_unowned_launch"):
        issues.append("UNOWNED_LAUNCH")
        exclusions.append("UNOWNED_LAUNCH")
    # Session wall intervals are not simulator-authority evidence. Attempts
    # serialize on the controller's global authority lock and carry their own
    # admission/settlement timestamps.
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

    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime: ControllerRuntime | None = None,
    repo_root: Path = REPO_ROOT,
) -> int:
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
