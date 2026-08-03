"""Fixed launcher and neutral result parser for the S1 script wrapper."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from aisle.harness.ablation import (
    AttemptResult,
    PreflightResult,
    normalize_safety_evidence,
    sha256_file,
)
from aisle.harness.reaper import reap_orphans
from aisle.harness.rollout import complete_violation_stream, instrumented_graph, parse_seed_range

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_GENESIS_BUILD_BUDGET_S = 420
_PER_EPISODE_BUDGET_S = 2100
_SIM_TIMEOUT_S = 600


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _spawn_script_dora(graph: Path, run_dir: Path, env: dict[str, str], stderr) -> subprocess.Popen:
    return subprocess.Popen(
        ["dora", "run", str(graph), "--uv"],
        cwd=run_dir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )


def _terminate_script(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=20)
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


_EPISODE_REQUIRED = frozenset(
    {
        "episode",
        "seed",
        "status",
        "failure",
        "t_end",
        "success",
        "penalties",
        "placement_scores",
        "goal_id",
        "verifier",
        "suite",
    }
)
_RETAIL_FAILURES = frozenset(
    {
        "misplaced",
        "misaligned",
        "overhang",
        "wrong_slot",
        "missing_item",
        "extra_item",
        "timeout",
    }
)
_PLACEMENT_SCORE_KEYS = frozenset(
    {"item", "slot", "pos", "yaw", "front_face", "overhang", "alignment"}
)


def _placement_score_is_valid(score: object) -> bool:
    return (
        isinstance(score, dict)
        and set(score) == _PLACEMENT_SCORE_KEYS
        and isinstance(score["item"], str)
        and bool(score["item"])
        and isinstance(score["slot"], str)
        and bool(score["slot"])
        and all(
            isinstance(score[criterion], bool)
            for criterion in ("pos", "yaw", "front_face", "overhang", "alignment")
        )
    )


def _finite_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _episode_record_is_valid(record: object, episode: int, seed: int) -> bool:
    if not isinstance(record, dict) or not _EPISODE_REQUIRED <= set(record):
        return False
    if (
        isinstance(record["episode"], bool)
        or not isinstance(record["episode"], int)
        or record["episode"] != episode
    ):
        return False
    if (
        isinstance(record["seed"], bool)
        or not isinstance(record["seed"], int)
        or record["seed"] != seed
    ):
        return False
    status = record["status"]
    success = record["success"]
    failure = record["failure"]
    if status not in ("success", "fail") or not isinstance(success, bool):
        return False
    if success != (status == "success"):
        return False
    if status == "success" and failure is not None:
        return False
    if status == "fail" and (not isinstance(failure, str) or not failure):
        return False
    if not _finite_nonnegative_number(record["t_end"]):
        return False
    penalties = record["penalties"]
    if not isinstance(penalties, list) or not all(
        isinstance(penalty, str) and penalty in _RETAIL_FAILURES for penalty in penalties
    ):
        return False
    if status == "success" and penalties:
        return False
    if status == "fail" and (not penalties or failure != penalties[0]):
        return False
    scores = record["placement_scores"]
    if not isinstance(scores, list) or not all(
        _placement_score_is_valid(score) for score in scores
    ):
        return False
    if record["goal_id"] != f"ep-{episode:04d}":
        return False
    if record["verifier"] != "oracle" or record["suite"] != "retail":
        return False
    return True


def _read_episode_results(path: Path, expected_seeds: list[int]) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    records: list[dict] = []
    malformed = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (OverflowError, TypeError, ValueError):
            malformed += 1
            continue
        expected_episode = len(records)
        try:
            valid = expected_episode < len(expected_seeds) and _episode_record_is_valid(
                record, expected_episode, expected_seeds[expected_episode]
            )
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            malformed += 1
            continue
        records.append(record)
    return records, malformed


def _trace_text(path: Path) -> list[str]:
    if not path.exists():
        return []
    import pyarrow as pa

    rows: list[str] = []
    try:
        with pa.ipc.open_stream(path) as reader:
            for batch in reader:
                rows.extend(
                    value for value in batch.column("text").to_pylist() if value is not None
                )
    except pa.ArrowInvalid:
        # A hard-killed recorder may leave one truncated tail batch. Complete
        # batches already appended above remain valid external evidence.
        pass
    return rows


def _extract_external_diagnostics(
    run_dir: Path, root: Path
) -> tuple[list[dict] | None, str | None]:
    traces = run_dir / "traces"
    violations = complete_violation_stream(traces / "budget-guard__violation.arrow")
    events = _trace_text(traces / "script-s1-runtime__policy_event.arrow")
    if not events:
        return violations, None
    policy_log = run_dir / "policy_events.jsonl"
    policy_log.write_text("".join(f"{event}\n" for event in events))
    return violations, str(policy_log.relative_to(root))


def _failure_counts(episodes: list[dict]) -> dict[str, int]:
    failures: dict[str, int] = {}
    for episode in episodes:
        if episode.get("status") == "success":
            continue
        failure = episode.get("failure") or "unknown"
        failures[str(failure)] = failures.get(str(failure), 0) + 1
    return failures


def run_script_rollout(
    policy: Path, seeds: str, run_id: str, *, root: Path | None = None
) -> AttemptResult:
    """Launch the fixed wrapper, parse its structured outputs, and clean up.

    This function intentionally performs no candidate preflight and no graph
    validation. ``preflight_script`` is the separate candidate gate; this
    runner executes only the repository-owned wrapper graph.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"unsafe run_id {run_id!r}")
    policy = policy.resolve()
    if not policy.is_file():
        raise ValueError(f"policy file does not exist: {policy}")
    seed_values = parse_seed_range(seeds)
    if not seed_values:
        raise ValueError("at least one seed is required")

    root = (root or _repository_root()).resolve()
    run_dir = root / "runs" / run_id
    if run_dir.exists():
        raise ValueError(f"run_id {run_id!r} already exists; refusing to overwrite")
    traces_dir = run_dir / "traces"
    traces_dir.mkdir(parents=True)
    wrapper = root / "graphs" / "ablation_script_s1_wrapper.yaml"
    exec_graph = instrumented_graph(wrapper, root, run_dir)
    results_path = run_dir / "episodes.jsonl"
    stderr_path = run_dir / "external_runtime.log"

    trusted_pythonpath = os.pathsep.join((str(root / "src"), str(root)))
    env = {
        **os.environ,
        "AISLE_SCRIPT_POLICY": str(policy),
        "AISLE_SEED": str(seed_values[0]),
        "AISLE_SEEDS": ",".join(str(seed) for seed in seed_values),
        "AISLE_TIER": "S1",
        "AISLE_EMBODIMENT": "mobile",
        "AISLE_TIMEOUT_S": str(_SIM_TIMEOUT_S),
        "AISLE_RESULTS": str(results_path),
        # The shared editable install may point at a parent checkout. Pin only
        # fixed trusted worktree code; never add the candidate directory.
        "PYTHONPATH": trusted_pythonpath,
    }

    started = time.monotonic()
    deadline = started + _GENESIS_BUILD_BUDGET_S + _PER_EPISODE_BUDGET_S * len(seed_values)
    proc: subprocess.Popen | None = None
    launch_error: str | None = None
    with stderr_path.open("w") as stderr:
        try:
            proc = _spawn_script_dora(exec_graph, run_dir, env, stderr)
            while time.monotonic() < deadline:
                completed, _ = _read_episode_results(results_path, seed_values)
                if len(completed) >= len(seed_values) or proc.poll() is not None:
                    break
                time.sleep(0.2)
        except OSError as exc:
            launch_error = f"STARTUP_FAILURE:{type(exc).__name__}"
        finally:
            if proc is not None:
                _terminate_script(proc)
            reap_orphans(run_dir)

    wall_s = time.monotonic() - started
    episodes, malformed = _read_episode_results(results_path, seed_values)
    failures = _failure_counts(episodes)
    if malformed:
        failures["RESULT_INVALID"] = malformed
    if launch_error is not None:
        failures[launch_error] = failures.get(launch_error, 0) + 1
    elif len(episodes) < len(seed_values) and not malformed:
        runtime_log = stderr_path.read_text(errors="replace")
        code = "COMMAND_INVALID" if "COMMAND_INVALID" in runtime_log else "ROLLOUT_INCOMPLETE"
        failures[code] = failures.get(code, 0) + 1

    violations, policy_log = _extract_external_diagnostics(run_dir, root)
    extra_items = sum(
        1
        for episode in episodes
        if episode.get("failure") == "extra_item" or episode.get("extra_item") is True
    )
    safety_record = (
        None
        if violations is None
        else {"ungated": 0, "clamps": len(violations), "extra_item": extra_items}
    )
    failures, safety = normalize_safety_evidence(failures, safety_record)
    artifacts = {"policy_log": policy_log} if policy_log is not None else {}
    return AttemptResult(
        attempt_id=run_id,
        candidate_hash=sha256_file(policy),
        # The adapter owns candidate preflight. A successful call into this
        # rollout-only boundary carries the neutral no-op preflight record.
        preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
        episodes=tuple(episodes),
        failures=failures,
        safety=safety,
        timing={
            "wall_s": wall_s,
            "sim_s": sum(float(episode.get("t_end", 0.0)) for episode in episodes),
        },
        artifacts=artifacts,
    )
