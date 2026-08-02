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


def _controlled_adapter_records(tmp_path: Path) -> list[dict]:
    driver = REPO_ROOT / "tests" / "fixtures" / "nodes" / "s1_starter_conformance_driver.py"
    recorder = REPO_ROOT / "tests" / "fixtures" / "nodes" / "recorder.py"
    typed = REPO_ROOT / "src" / "aisle" / "nodes" / "s1_ablation_driver.py"
    script = REPO_ROOT / "src" / "aisle" / "nodes" / "script_s1_runtime.py"
    record_out = tmp_path / "adapter-intents.jsonl"
    graph = tmp_path / "controlled-starters.yaml"
    graph.write_text(
        yaml.safe_dump(
            {
                "nodes": [
                    {
                        "id": "driver",
                        "path": str(driver),
                        "inputs": {"tick": "dora/timer/millis/50"},
                        "outputs": [
                            "reset_done",
                            "episode_goal",
                            "order",
                            "subtask_plan",
                            "poses",
                            "joint_state",
                            "base_pose",
                            "nav_result",
                        ],
                    },
                    {
                        "id": "typed",
                        "path": str(typed),
                        "inputs": {
                            "reset_done": {"source": "driver/reset_done", "queue_size": 100},
                            "order": {"source": "driver/order", "queue_size": 100},
                            "subtask_plan": {
                                "source": "driver/subtask_plan",
                                "queue_size": 100,
                            },
                            "poses": {"source": "driver/poses", "queue_size": 100},
                            "joint_state": {
                                "source": "driver/joint_state",
                                "queue_size": 100,
                            },
                            "base_pose": {"source": "driver/base_pose", "queue_size": 100},
                            "nav_result": {"source": "driver/nav_result", "queue_size": 100},
                        },
                        "outputs": ["nav_goal", "joint_cmd", "gripper_cmd"],
                        "env": {"AISLE_SEED": "17"},
                    },
                    {
                        "id": "script",
                        "path": str(script),
                        "inputs": {
                            "reset_done": {"source": "driver/reset_done", "queue_size": 100},
                            "episode_goal": {
                                "source": "driver/episode_goal",
                                "queue_size": 100,
                            },
                            "poses": {"source": "driver/poses", "queue_size": 100},
                            "joint_state": {
                                "source": "driver/joint_state",
                                "queue_size": 100,
                            },
                            "base_pose": {"source": "driver/base_pose", "queue_size": 100},
                            "nav_result": {"source": "driver/nav_result", "queue_size": 100},
                        },
                        "outputs": ["nav_goal", "joint_cmd", "gripper_cmd", "policy_event"],
                        "env": {
                            "AISLE_SEED": "17",
                            "AISLE_SCRIPT_POLICY": str(
                                (REPO_ROOT / "baselines" / "script_s1" / "starter.py").resolve()
                            ),
                        },
                    },
                    {
                        "id": "recorder",
                        "path": str(recorder),
                        "inputs": {
                            "typed_nav": {"source": "typed/nav_goal", "queue_size": 100},
                            "typed_gripper": {
                                "source": "typed/gripper_cmd",
                                "queue_size": 100,
                            },
                            "script_nav": {"source": "script/nav_goal", "queue_size": 100},
                            "script_gripper": {
                                "source": "script/gripper_cmd",
                                "queue_size": 100,
                            },
                        },
                        "env": {
                            "RECORDER_OUT": str(record_out),
                            "RECORDER_DURATION_S": "10",
                        },
                    },
                ]
            },
            sort_keys=False,
        )
    )
    env = {
        **os.environ,
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
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if record_out.exists() and len(record_out.read_text().splitlines()) >= 8:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            _, stderr = proc.communicate()
        except ProcessLookupError:
            _, stderr = proc.communicate()
        from aisle.harness.reaper import reap_orphans

        reap_orphans(tmp_path)
    records = (
        [json.loads(line) for line in record_out.read_text().splitlines()]
        if record_out.exists()
        else []
    )
    assert len(records) >= 8, stderr[-3000:]
    return records


@pytest.mark.graph
@pytest.mark.skipif(shutil.which("dora") is None, reason="dora CLI not installed")
def test_controlled_dataflow_matches_all_four_serialized_action_intents(tmp_path: Path):
    """CON-5: actual typed/script adapters serialize the same four starter actions."""
    records = _controlled_adapter_records(tmp_path)
    expected = [
        {"action": "nav", "target": "shelf_zone_A"},
        {"action": "pick", "target": "A1-L1-S0#0"},
        {"action": "nav", "target": "counter"},
        {"action": "place", "target": "A1-L1-S0#0"},
    ]
    for representation in ("typed", "script"):
        emitted = [
            record
            for record in records
            if record["id"] in {f"{representation}_nav", f"{representation}_gripper"}
        ]
        emitted.sort(key=lambda record: record["metadata"]["intent_seq"])
        assert [
            {
                "action": record["metadata"]["intent_action"],
                "target": record["metadata"]["intent_target"],
            }
            for record in emitted
        ] == expected
        assert [json.loads(record["text"]) for record in emitted if "text" in record] == [
            {"location": "shelf_zone_A"},
            {"location": "counter"},
        ]
        assert [record["values"] for record in emitted if "values" in record] == [
            [1.0],
            [0.0],
        ]


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
    canonical_oracle = json.dumps(
        initial,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
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
