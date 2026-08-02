"""Static graph conformance for the typed S1 ablation starter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _nodes(name: str) -> dict[str, dict]:
    document = yaml.safe_load((REPO_ROOT / "graphs" / name).read_text())
    return {node["id"]: node for node in document["nodes"]}


@pytest.mark.unit
def test_typed_starter_exposes_the_required_behavior_nodes():
    """CAP-5: typed treatment exposes order, planning, navigation, and driver nodes."""
    nodes = _nodes("ablation_s1_starter.yaml")

    assert {"order-reader", "task-planner", "waypoint-nav", "s1-ablation-driver"} <= set(nodes)
    assert nodes["s1-ablation-driver"]["inputs"]["order"]["source"] == "order-reader/order"
    assert nodes["s1-ablation-driver"]["inputs"]["subtask_plan"]["source"] == (
        "task-planner/subtask_plan"
    )
    assert nodes["waypoint-nav"]["inputs"]["nav_goal"]["source"] == ("s1-ablation-driver/nav_goal")


@pytest.mark.unit
def test_typed_starter_preserves_the_reviewed_safety_topology():
    """BG-1, MOB-3, CON-7: every typed-starter motion path remains guard-interposed."""
    nodes = _nodes("ablation_s1_starter.yaml")

    bridge = nodes["dora-genesis"]["inputs"]
    guard = nodes["budget-guard"]["inputs"]
    assert bridge["joint_cmd"]["source"] == "budget-guard/joint_cmd_safe"
    assert bridge["gripper_cmd"]["source"] == "budget-guard/gripper_cmd_safe"
    assert bridge["base_cmd"]["source"] == "budget-guard/base_cmd_safe"
    assert guard["joint_cmd"]["source"] == "s1-ablation-driver/joint_cmd"
    assert guard["gripper_cmd"]["source"] == "s1-ablation-driver/gripper_cmd"
    assert guard["base_cmd"]["source"] == "waypoint-nav/base_cmd"


@pytest.mark.unit
def test_typed_and_script_starters_share_fixed_runtime_boundaries():
    """CON-7: both treatments use identical scene, reset, guard, nav, verifier, and client."""
    typed = _nodes("ablation_s1_starter.yaml")
    script = _nodes("ablation_script_s1_wrapper.yaml")
    fixed = {
        "dora-genesis",
        "reset",
        "budget-guard",
        "waypoint-nav",
        "verifier-retail",
        "rollout-client",
    }

    assert all(typed[node_id]["path"] == script[node_id]["path"] for node_id in fixed)
    assert typed["dora-genesis"]["env"] == script["dora-genesis"]["env"]
    assert typed["budget-guard"]["env"] == script["budget-guard"]["env"]
    assert typed["verifier-retail"]["inputs"] == script["verifier-retail"]["inputs"]


@pytest.mark.unit
def test_typed_driver_manifest_matches_the_graph_boundary():
    """CAP-1, CAP-5: the fixed driver manifest describes its executable graph ports."""
    manifest = yaml.safe_load(
        (REPO_ROOT / "registry" / "manifests" / "s1-ablation-driver.yaml").read_text()
    )
    driver = _nodes("ablation_s1_starter.yaml")["s1-ablation-driver"]

    assert manifest["id"] == "s1-ablation-driver"
    assert manifest["source"] == "src/aisle/nodes/s1_ablation_driver.py"
    assert set(manifest["inputs"]) == set(driver["inputs"])
    assert set(manifest["outputs"]) == set(driver["outputs"])


def _trace_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with pa.ipc.open_stream(path) as reader:
        try:
            for batch in reader:
                rows.extend(
                    {
                        "sim_time_ns": batch.column("sim_time_ns")[index].as_py(),
                        "data": batch.column("data")[index].as_py(),
                        "text": batch.column("text")[index].as_py(),
                    }
                    for index in range(batch.num_rows)
                )
        except pa.ArrowInvalid:
            pass
    return rows


def _live_initial_fingerprint(tmp_path: Path, graph_name: str) -> dict:
    from aisle.harness.reaper import reap_orphans
    from aisle.harness.rollout import instrumented_graph

    run_dir = tmp_path / Path(graph_name).stem
    trace_dir = run_dir / "traces"
    trace_dir.mkdir(parents=True)
    graph = instrumented_graph(
        REPO_ROOT / "graphs" / graph_name,
        REPO_ROOT,
        run_dir,
        trace_dir=trace_dir,
    )
    env = {
        **os.environ,
        "AISLE_SEED": "0",
        "AISLE_SEEDS": "0",
        "AISLE_TIER": "S1",
        "AISLE_EMBODIMENT": "mobile",
        "AISLE_TIMEOUT_S": "2",
        "AISLE_RESULTS": str(run_dir / "episodes.jsonl"),
        "PYTHONPATH": os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT))),
    }
    if graph_name == "ablation_script_s1_wrapper.yaml":
        env["AISLE_SCRIPT_POLICY"] = str(
            (REPO_ROOT / "baselines" / "script_s1" / "starter.py").resolve()
        )
        intent_node = "script-s1-runtime"
    else:
        intent_node = "s1-ablation-driver"

    stderr_path = run_dir / "runtime.log"
    with stderr_path.open("w") as stderr:
        proc = subprocess.Popen(
            ["dora", "run", str(graph), "--uv"],
            cwd=run_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 480
        try:
            while time.monotonic() < deadline:
                goals = _trace_rows(trace_dir / "rollout-client__episode_goal.arrow")
                oracle = _trace_rows(trace_dir / "dora-genesis__oracle_state.arrow")
                nav = _trace_rows(trace_dir / f"{intent_node}__nav_goal.arrow")
                ready = False
                if goals and oracle and nav:
                    reset_sim_ns = json.loads(goals[0]["text"])["reset_sim_ns"]
                    ready = any(row["sim_time_ns"] >= reset_sim_ns for row in oracle)
                if ready or proc.poll() is not None:
                    break
                time.sleep(1)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            except ProcessLookupError:
                proc.wait()
            reap_orphans(run_dir)

    goals = _trace_rows(trace_dir / "rollout-client__episode_goal.arrow")
    oracle = _trace_rows(trace_dir / "dora-genesis__oracle_state.arrow")
    nav = _trace_rows(trace_dir / f"{intent_node}__nav_goal.arrow")
    assert goals and oracle and nav, stderr_path.read_text(errors="replace")[-3000:]
    goal = json.loads(goals[0]["text"])
    initial = next(row["data"] for row in oracle if row["sim_time_ns"] >= goal["reset_sim_ns"])
    nav_goal = json.loads(nav[0]["text"])
    canonical_goal = json.dumps(goal, sort_keys=True, separators=(",", ":")).encode()
    canonical_oracle = json.dumps(initial, separators=(",", ":")).encode()
    return {
        "goal_hash": hashlib.sha256(canonical_goal).hexdigest(),
        "oracle_digest": hashlib.sha256(canonical_oracle).hexdigest(),
        "seed": goal["seed"],
        "nav_goal": nav_goal,
    }


@pytest.mark.graph
@pytest.mark.skipif(
    importlib.util.find_spec("genesis") is None or shutil.which("dora") is None,
    reason="sim extra or dora CLI not installed",
)
def test_live_same_seed_starters_share_initial_state_and_intent(tmp_path: Path):
    """CON-5, CON-7: bounded sequential launches match goal, oracle, and first intent."""
    from aisle.harness.s1_ablation_common import canonical_action_intent

    typed = _live_initial_fingerprint(tmp_path, "ablation_s1_starter.yaml")
    script = _live_initial_fingerprint(tmp_path, "ablation_script_s1_wrapper.yaml")
    assert typed["goal_hash"] == script["goal_hash"]
    assert typed["oracle_digest"] == script["oracle_digest"]
    assert typed["seed"] == script["seed"] == 0
    assert canonical_action_intent(
        [{"kind": "nav_goal", "payload": typed["nav_goal"]}]
    ) == canonical_action_intent([{"kind": "nav_goal", "payload": script["nav_goal"]}])

    typed_nodes = _nodes("ablation_s1_starter.yaml")
    script_nodes = _nodes("ablation_script_s1_wrapper.yaml")
    assert typed_nodes["budget-guard"]["env"] == script_nodes["budget-guard"]["env"]
    assert typed_nodes["verifier-retail"] == script_nodes["verifier-retail"]
    print(json.dumps({"typed": typed, "script": script}, sort_keys=True))
