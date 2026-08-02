"""Live safety smoke for the fixed S1 script-policy wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

pytestmark = [
    pytest.mark.graph,
    pytest.mark.skipif(
        importlib.util.find_spec("genesis") is None or shutil.which("dora") is None,
        reason="sim extra or dora CLI not installed",
    ),
]


def _numeric_rows(path: Path) -> list[list[float]]:
    rows: list[list[float]] = []
    with pa.ipc.open_stream(path) as reader:
        try:
            for batch in reader:
                rows.extend(
                    value for value in batch.column("data").to_pylist() if value is not None
                )
        except pa.ArrowInvalid:
            pass
    return rows


def _text_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with pa.ipc.open_stream(path) as reader:
        try:
            for batch in reader:
                rows.extend(
                    json.loads(value)
                    for value in batch.column("text").to_pylist()
                    if value is not None
                )
        except pa.ArrowInvalid:
            pass
    return rows


def test_script_starter_guard_clamps_before_genesis(tmp_path: Path):
    """BG-1..3, MOB-3, CON-7: one starter run yields a result or documented
    task failure, and its guard-violating close request cannot reach Genesis
    unmodified."""
    from aisle.harness.rollout import instrumented_graph

    wrapper = REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml"
    results = tmp_path / "episodes.jsonl"
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    graph = instrumented_graph(wrapper, REPO_ROOT, tmp_path, trace_dir=trace_dir)
    env = {
        **os.environ,
        "AISLE_SCRIPT_POLICY": str((REPO_ROOT / "baselines/script_s1/starter.py").resolve()),
        "AISLE_SEED": "0",
        "AISLE_SEEDS": "0",
        "AISLE_TIER": "S1",
        "AISLE_EMBODIMENT": "mobile",
        # Included in the goal for interface parity. verifier-retail correctly
        # keeps its frozen placement.toml deadline instead.
        "AISLE_TIMEOUT_S": "2",
        "AISLE_RESULTS": str(results),
        "PYTHONPATH": os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT))),
    }
    proc = subprocess.Popen(
        ["dora", "run", str(graph), "--uv"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    requested_trace = trace_dir / "script-s1-runtime__gripper_cmd.arrow"
    safe_trace = trace_dir / "budget-guard__gripper_cmd_safe.arrow"
    violation_trace = trace_dir / "budget-guard__violation.arrow"
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            if results.exists() and results.read_bytes().count(b"\n") >= 1:
                break
            if all(
                path.exists() and path.stat().st_size > 0
                for path in (
                    requested_trace,
                    safe_trace,
                    violation_trace,
                )
            ):
                # The safety behavior under test has completed. The frozen
                # retail verifier's 600 sim-second deadline can take hours of
                # wall time for this intentionally incomplete starter.
                break
            if proc.poll() is not None:
                break
            time.sleep(1)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        except ProcessLookupError:
            proc.communicate()
        from conftest import _reap_orphan_nodes

        _reap_orphan_nodes(tmp_path)

    episodes = (
        [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
        if results.exists()
        else []
    )
    if episodes:
        assert len(episodes) == 1
        assert episodes[0]["status"] in {"success", "fail"}
    else:
        # Documented task failure permitted by the Task 4 smoke criterion:
        # the starter's `target` nav API is intentionally incomplete, while
        # verifier-retail uses the frozen 600-sim-second placement deadline.
        from aisle.verifier.retail import load_placement

        nav_requests = _text_rows(trace_dir / "script-s1-runtime__nav_goal.arrow")
        assert load_placement()["episode"]["timeout_s"] == 600.0
        assert nav_requests and "target" in nav_requests[0]
        assert not ({"location", "pose"} & set(nav_requests[0]))

    requested = _numeric_rows(requested_trace)
    safe = _numeric_rows(safe_trace)
    violations = _text_rows(violation_trace)
    assert requested and requested[0] == [1.0]
    assert safe and safe[0] != requested[0]
    assert any(violation["reason"] in {"position", "velocity"} for violation in violations)

    graph_nodes = {node["id"]: node for node in yaml.safe_load(graph.read_text())["nodes"]}
    assert graph_nodes["dora-genesis"]["inputs"]["gripper_cmd"]["source"] == (
        "budget-guard/gripper_cmd_safe"
    )
