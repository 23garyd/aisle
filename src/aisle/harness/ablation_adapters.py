"""Neutral controller-facing adapters for the S1 harness/script ablation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from aisle.harness.ablation import AttemptResult, PreflightResult, SafetyResult, sha256_file
from aisle.harness.rollout import parse_seed_range
from aisle.harness.script_preflight import preflight_script
from aisle.harness.script_rollout import run_script_rollout

_COMMAND_TIMEOUT_S = 30.0
_GENESIS_BUILD_BUDGET_S = 420.0
_S1_EPISODE_BUDGET_S = 2100.0
_ZERO_SAFETY = SafetyResult(ungated=0, clamps=0, extra_item=0)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
ScriptPreflightRunner = Callable[[Path], PreflightResult]
ScriptRolloutRunner = Callable[[Path, str, str], AttemptResult]


class ConditionAdapter(Protocol):
    """The sole condition-specific interface consumed by the controller."""

    def preflight(self, candidate: Path) -> PreflightResult: ...

    def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult: ...

    def collect_deliverable(self, candidate: Path) -> dict[str, str]: ...


def _run_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _parse_single_json(stdout: object) -> dict | None:
    """Parse exactly one object, rejecting concatenated output and scalars."""
    if not isinstance(stdout, str):
        return None
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stdout.lstrip())
    except json.JSONDecodeError:
        return None
    if stdout.lstrip()[end:].strip() or not isinstance(value, dict):
        return None
    return value


def _command_response(
    runner: CommandRunner, argv: list[str], timeout: float = _COMMAND_TIMEOUT_S
) -> tuple[dict | None, str | None]:
    """Run an argv command and enforce the shared CLI response contract."""
    try:
        completed = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        return None, "INFRA_TIMEOUT"
    except OSError:
        return None, "INFRA_LAUNCH"
    response = _parse_single_json(completed.stdout)
    if response is None or not isinstance(response.get("ok"), bool):
        return None, "INFRA_PROTOCOL"
    if (completed.returncode == 0) != response["ok"]:
        return None, "INFRA_PROTOCOL"
    return response, None


def _preflight_failure(code: str, started: float) -> PreflightResult:
    return PreflightResult(ok=False, errors=({"code": code},), wall_s=time.monotonic() - started)


def _normal_preflight(response: dict, started: float) -> PreflightResult:
    errors = response.get("errors", [])
    if not isinstance(errors, list) or not all(isinstance(error, dict) for error in errors):
        return _preflight_failure("INFRA_PROTOCOL", started)
    return PreflightResult(
        ok=response["ok"],
        errors=tuple(dict(error) for error in errors),
        wall_s=time.monotonic() - started,
    )


def _attempt_failure(
    run_id: str, candidate_hash: str, preflight: PreflightResult, code: str, started: float
) -> AttemptResult:
    return AttemptResult(
        attempt_id=run_id,
        candidate_hash=candidate_hash,
        preflight=preflight,
        episodes=(),
        failures={code: 1},
        safety=_ZERO_SAFETY,
        timing={"wall_s": time.monotonic() - started, "sim_s": 0.0},
        artifacts={},
    )


def _candidate_hash(candidate: Path) -> tuple[str, bool]:
    """Return a byte hash, or a deterministic missing-input identity for refusals."""
    try:
        return sha256_file(candidate), True
    except OSError:
        return hashlib.sha256(f"missing-candidate:{candidate}".encode()).hexdigest(), False


def _valid_rollout_inputs(seeds: str, run_id: str) -> bool:
    try:
        return bool(parse_seed_range(seeds)) and bool(_RUN_ID.fullmatch(run_id))
    except (AttributeError, TypeError, ValueError):
        return False


def _artifacts_from_rollout(response: dict) -> dict[str, str]:
    raw = response.get("artifacts")
    if raw is not None:
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ValueError("artifacts")
        return dict(raw)
    artifacts: dict[str, str] = {}
    if isinstance(response.get("traces_dir"), str):
        artifacts["traces_dir"] = response["traces_dir"]
    videos = response.get("videos", [])
    if not isinstance(videos, list) or not all(isinstance(video, str) for video in videos):
        raise ValueError("videos")
    artifacts.update({f"video_{index}": video for index, video in enumerate(videos)})
    return artifacts


def _normalize_rollout(
    response: dict, run_id: str, candidate_hash: str, preflight: PreflightResult
) -> AttemptResult:
    """Translate either arm's rollout report into the single canonical schema."""
    try:
        episodes = response["episodes"]
        failures = response["failures"]
        safety = response["safety"]
        timing = response.get("durations", response.get("timing"))
        if not isinstance(timing, dict):
            raise ValueError("timing")
        return AttemptResult.from_dict(
            {
                "attempt_id": run_id,
                "candidate_hash": candidate_hash,
                "preflight": preflight.to_dict(),
                "episodes": episodes,
                "failures": failures,
                "safety": safety,
                "timing": timing,
                "artifacts": _artifacts_from_rollout(response),
            }
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("INFRA_PROTOCOL") from None


class AisleAdapter:
    """Run a graph through exactly the public AISLE validation and rollout CLIs."""

    def __init__(self, root: Path, *, runner: CommandRunner = _run_command) -> None:
        self._root = root.resolve()
        self._runner = runner
        self._artifacts: dict[Path, dict[str, str]] = {}
        self._preflights: dict[Path, tuple[str, PreflightResult]] = {}

    def _candidate(self, candidate: Path) -> Path:
        return candidate.resolve()

    def preflight(self, candidate: Path) -> PreflightResult:
        started = time.monotonic()
        response, code = _command_response(
            self._runner,
            [
                "harness",
                "validate",
                str(self._candidate(candidate)),
                "--root",
                str(self._root),
                "--embodiment",
                "mobile",
            ],
        )
        if code is not None:
            result = _preflight_failure(code, started)
        else:
            assert response is not None
            result = _normal_preflight(response, started)
        try:
            self._preflights[self._candidate(candidate)] = (
                sha256_file(self._candidate(candidate)),
                result,
            )
        except OSError:
            pass
        return result

    def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
        started = time.monotonic()
        candidate_path = self._candidate(candidate)
        candidate_hash, candidate_exists = _candidate_hash(candidate_path)
        if not candidate_exists:
            preflight = PreflightResult(ok=False, errors=({"code": "INFRA_ARGUMENT"},), wall_s=0.0)
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_ARGUMENT", started)
        cached = self._preflights.get(candidate_path)
        preflight = (
            cached[1]
            if cached is not None and cached[0] == candidate_hash
            else self.preflight(candidate_path)
        )
        if not preflight.ok:
            return _attempt_failure(run_id, candidate_hash, preflight, "PREFLIGHT_FAILED", started)
        if not _valid_rollout_inputs(seeds, run_id):
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_ARGUMENT", started)
        episode_count = len(parse_seed_range(seeds))
        response, code = _command_response(
            self._runner,
            [
                "harness",
                "rollout",
                "--graph",
                str(candidate_path),
                "--tier",
                "S1",
                "--embodiment",
                "mobile",
                "--episodes",
                str(episode_count),
                "--seeds",
                seeds,
                "--reset",
                "teleport",
                "--verifier",
                "oracle",
                "--root",
                str(self._root),
                "--no-idea-gate",
                "--run-id",
                run_id,
            ],
            _GENESIS_BUILD_BUDGET_S + _S1_EPISODE_BUDGET_S * episode_count,
        )
        if code is not None:
            return _attempt_failure(run_id, candidate_hash, preflight, code, started)
        assert response is not None
        if not isinstance(response.get("safety"), dict):
            return _attempt_failure(
                run_id, candidate_hash, preflight, "INFRA_SAFETY_UNAVAILABLE", started
            )
        try:
            result = _normalize_rollout(response, run_id, candidate_hash, preflight)
        except ValueError:
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_PROTOCOL", started)
        self._artifacts[candidate_path] = dict(result.artifacts)
        return result

    def collect_deliverable(self, candidate: Path) -> dict[str, str]:
        return dict(self._artifacts.get(self._candidate(candidate), {}))


class ScriptAdapter:
    """Run a policy through the isolated preflight and protected wrapper runner."""

    def __init__(
        self,
        *,
        preflight_runner: ScriptPreflightRunner = preflight_script,
        rollout_runner: ScriptRolloutRunner = run_script_rollout,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._preflight_runner = preflight_runner
        self._rollout_runner = rollout_runner
        self._clock = clock
        self._policy_logs: dict[Path, str] = {}
        self._preflights: dict[Path, tuple[str, PreflightResult]] = {}

    def _candidate(self, candidate: Path) -> Path:
        return candidate.resolve()

    def preflight(self, candidate: Path) -> PreflightResult:
        candidate_path = self._candidate(candidate)
        started = self._clock()
        try:
            result = self._preflight_runner(candidate_path)
        except subprocess.TimeoutExpired:
            result = PreflightResult(
                ok=False,
                errors=({"code": "INFRA_TIMEOUT"},),
                wall_s=self._clock() - started,
            )
        except OSError:
            result = PreflightResult(
                ok=False,
                errors=({"code": "INFRA_LAUNCH"},),
                wall_s=self._clock() - started,
            )
        try:
            self._preflights[candidate_path] = (sha256_file(candidate_path), result)
        except OSError:
            pass
        return result

    def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
        started = time.monotonic()
        candidate_path = self._candidate(candidate)
        candidate_hash, candidate_exists = _candidate_hash(candidate_path)
        if not candidate_exists:
            preflight = PreflightResult(ok=False, errors=({"code": "INFRA_ARGUMENT"},), wall_s=0.0)
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_ARGUMENT", started)
        cached = self._preflights.get(candidate_path)
        preflight = (
            cached[1]
            if cached is not None and cached[0] == candidate_hash
            else self.preflight(candidate_path)
        )
        if not preflight.ok:
            return _attempt_failure(run_id, candidate_hash, preflight, "PREFLIGHT_FAILED", started)
        if not _valid_rollout_inputs(seeds, run_id):
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_ARGUMENT", started)
        try:
            raw = self._rollout_runner(candidate_path, seeds, run_id)
        except subprocess.TimeoutExpired:
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_TIMEOUT", started)
        except OSError:
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_LAUNCH", started)
        except ValueError:
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_ARGUMENT", started)
        try:
            result = _normalize_rollout(raw.to_dict(), run_id, candidate_hash, preflight)
        except (AttributeError, ValueError):
            return _attempt_failure(run_id, candidate_hash, preflight, "INFRA_PROTOCOL", started)
        policy_log = result.artifacts.get("policy_log")
        if policy_log is not None:
            self._policy_logs[candidate_path] = policy_log
        return result

    def collect_deliverable(self, candidate: Path) -> dict[str, str]:
        """Expose the structured policy event log, never the raw trace store."""
        policy_log = self._policy_logs.get(self._candidate(candidate))
        return {} if policy_log is None else {"policy_log": policy_log}
