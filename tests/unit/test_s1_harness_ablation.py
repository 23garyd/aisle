"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

REPO_ROOT = Path(__file__).parents[2]


def _attempt() -> dict:
    return {
        "attempt_id": "A-0001",
        "candidate_hash": "a" * 64,
        "preflight": {"ok": True, "errors": [], "wall_s": 0.25},
        "episodes": [],
        "failures": {},
        "safety": {"ungated": 0, "clamps": 0, "extra_item": 0},
        "timing": {"wall_s": 1.0, "sim_s": 0.0},
        "artifacts": {},
    }


def _run_script_preflight_worker(path: Path) -> tuple[int, dict, str]:
    """Run the isolated worker logic and return its exact stdout JSON result."""
    from aisle.harness.script_preflight_worker import main

    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = main([str(path.resolve())])
    output = stdout.getvalue()
    return exit_code, json.loads(output), output


def test_attempt_result_round_trip_and_rejects_unknown_fields():
    """HAR-1, CON-5: both ablation arms emit one canonical attempt record."""
    from aisle.harness.ablation import AttemptResult

    raw = _attempt()
    assert AttemptResult.from_dict(raw).to_dict() == raw
    with pytest.raises(ValueError, match="unknown"):
        AttemptResult.from_dict({**raw, "condition": "aisle"})


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("candidate_hash", "not-a-sha", "candidate_hash"),
        ("preflight", {"ok": True, "errors": [], "wall_s": -0.01}, "wall_s"),
        ("preflight", {"ok": True, "errors": [], "wall_s": float("nan")}, "wall_s"),
        ("episodes", {}, "episodes"),
        ("safety", {"ungated": -1, "clamps": 0, "extra_item": 0}, "ungated"),
        ("safety", {"ungated": 0, "clamps": -1, "extra_item": 0}, "clamps"),
        ("safety", {"ungated": 0, "clamps": 0, "extra_item": -1}, "extra_item"),
        ("timing", {"wall_s": -1.0, "sim_s": 0.0}, "wall_s"),
        ("timing", {"wall_s": 1.0, "sim_s": float("inf")}, "sim_s"),
    ],
)
def test_attempt_result_rejects_malformed_values(field: str, value: object, error: str):
    """HAR-1, CON-5: invalid neutral attempt data cannot enter the result ledger."""
    from aisle.harness.ablation import AttemptResult

    raw = _attempt()
    raw[field] = value

    with pytest.raises(ValueError, match=error):
        AttemptResult.from_dict(raw)


def test_paired_assignments_are_seeded_and_balanced():
    """CON-5: seeded paired blocks assign each condition exactly once."""
    from aisle.harness.ablation import paired_assignments

    assignments = paired_assignments(seed=1, pairs=8)

    assert assignments == paired_assignments(seed=1, pairs=8)
    assert assignments != paired_assignments(seed=2, pairs=8)
    assert len(assignments) == 16
    assert all(
        set(assignments[index : index + 2]) == {"aisle", "script"}
        for index in range(0, len(assignments), 2)
    )


def test_sha256_file_hashes_exact_file_bytes(tmp_path: Path):
    """CON-5: artifact identities hash bytes, not platform text decoding."""
    from aisle.harness.ablation import sha256_file

    path = tmp_path / "candidate.py"
    path.write_bytes(b"aisle\n")

    assert sha256_file(path) == "77f7420162f5fc97aeb5c147ced4b7b67f4bbc632a2c53ac53d353c603e12399"


def test_session_ledger_is_canonical_hash_chained_and_tamper_evident(tmp_path: Path):
    """HAR-1, CON-5: immutable session events verify as one hash chain."""
    from aisle.harness.ablation import append_ledger, verify_ledger

    path = tmp_path / "session.jsonl"
    first = append_ledger(path, {"kind": "session_start", "session": "P01-A"})
    second = append_ledger(path, {"kind": "attempt", "attempt": "A-0001"})
    head = append_ledger(path, {"kind": "session_end", "status": "agent_done"})

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert [record["prev_sha256"] for record in records] == [None, first, second]
    assert all(
        line == json.dumps(record, sort_keys=True, separators=(",", ":"))
        for line, record in zip(path.read_text().splitlines(), records, strict=True)
    )
    assert verify_ledger(path) == (True, head)

    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace("A-0001", "A-0002")
    path.write_text("\n".join(lines) + "\n")

    assert verify_ledger(path) == (False, None)


def test_ledger_rejects_nonfinite_event_values(tmp_path: Path):
    """CON-5: ledger events remain valid interoperable JSON."""
    from aisle.harness.ablation import append_ledger

    with pytest.raises(ValueError, match="Out of range float values"):
        append_ledger(tmp_path / "session.jsonl", {"value": float("nan")})


def test_ledger_invalid_utf8_tampering_returns_invalid_result(tmp_path: Path):
    """CON-5: byte-level ledger tampering cannot escape verification."""
    from aisle.harness.ablation import append_ledger, verify_ledger

    path = tmp_path / "session.jsonl"
    append_ledger(path, {"kind": "session_start"})
    path.write_bytes(b"\xff")

    assert verify_ledger(path) == (False, None)


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def create_policy(:\n", "SCRIPT_SYNTAX"),
        ("import does_not_exist\n", "SCRIPT_IMPORT"),
        ("VALUE = 1\n", "FACTORY_MISSING"),
        ("def create_policy(seed):\n    return object()\n", "POLICY_INVALID"),
        (
            "from baselines.script_s1.contract import PolicyCommand\n"
            "class Policy:\n"
            "    def on_event(self, event):\n"
            "        return [PolicyCommand('bad_command', {})]\n"
            "def create_policy(seed):\n"
            "    return Policy()\n",
            "COMMAND_INVALID",
        ),
    ],
)
def test_script_preflight_returns_stable_failure_codes(tmp_path: Path, source: str, code: str):
    """HAR-1, CON-8: invalid script candidates fail before any runtime launch."""
    path = tmp_path / "candidate.py"
    path.write_text(source)

    exit_code, result, _ = _run_script_preflight_worker(path)

    assert exit_code == 1
    assert result == {"code": code, "ok": False}


def test_script_preflight_accepts_a_valid_policy(tmp_path: Path):
    """HAR-1, CON-8: a policy with a valid synthetic-goal command passes preflight."""
    path = tmp_path / "candidate.py"
    path.write_text(
        "from baselines.script_s1.contract import PolicyCommand\n"
        "class Policy:\n"
        "    def on_event(self, event):\n"
        "        if event.kind == 'episode_goal':\n"
        "            return [PolicyCommand('nav_goal', {'target': [0.0, 1.0]})]\n"
        "        return []\n"
        "def create_policy(seed):\n"
        "    return Policy()\n"
    )

    exit_code, result, _ = _run_script_preflight_worker(path)

    assert exit_code == 0
    assert result == {"ok": True}


def test_script_preflight_accepts_the_editable_starter():
    """HAR-1, CON-8: the shipped starter obeys the isolated policy contract."""
    exit_code, result, _ = _run_script_preflight_worker(Path("baselines/script_s1/starter.py"))

    assert exit_code == 0
    assert result == {"ok": True}


def test_script_preflight_worker_uses_the_required_isolated_module_argv(tmp_path: Path):
    """HAR-1, CON-8: preflight invokes its worker via the fixed isolated module form."""
    from aisle.harness.script_preflight import _worker_command

    path = tmp_path / "candidate.py"

    assert _worker_command(path) == [
        sys.executable,
        "-I",
        "-m",
        "aisle.harness.script_preflight_worker",
        str(path.resolve()),
    ]


def test_script_preflight_accepts_policy_stdout_noise(tmp_path: Path):
    """HAR-1, CON-8: candidate prints cannot corrupt the worker's JSON result."""
    path = tmp_path / "candidate.py"
    path.write_text(
        "from baselines.script_s1.contract import PolicyCommand\n"
        "print('import-noise')\n"
        "class Policy:\n"
        "    def on_event(self, event):\n"
        "        print('event-noise')\n"
        "        return [PolicyCommand('nav_goal', {})]\n"
        "def create_policy(seed):\n"
        "    print('factory-noise')\n"
        "    return Policy()\n"
    )

    exit_code, result, output = _run_script_preflight_worker(path)

    assert exit_code == 0
    assert result == {"ok": True}
    assert output == '{"ok":true}\n'


def _graph_nodes(path: Path) -> dict[str, dict]:
    document = yaml.safe_load(path.read_text())
    return {node["id"]: node for node in document["nodes"]}


def test_script_wrapper_keeps_oracle_state_outside_the_editable_policy():
    """CON-7: the script policy receives observations but never privileged oracle_state."""
    nodes = _graph_nodes(REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml")

    runtime = nodes["script-s1-runtime"]
    assert set(runtime["inputs"]) == {
        "episode_goal",
        "poses",
        "joint_state",
        "base_pose",
        "nav_result",
        "reset_done",
    }
    assert all(
        source.get("source") != "dora-genesis/oracle_state" for source in runtime["inputs"].values()
    )
    assert nodes["verifier-retail"]["inputs"]["oracle_state"]["source"] == (
        "dora-genesis/oracle_state"
    )


def test_script_wrapper_routes_every_motion_command_through_budget_guard():
    """BG-1, MOB-3: script arm and navigation base motion reach Genesis only via the guard."""
    nodes = _graph_nodes(REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml")

    bridge_inputs = nodes["dora-genesis"]["inputs"]
    guard_inputs = nodes["budget-guard"]["inputs"]
    assert bridge_inputs["joint_cmd"]["source"] == "budget-guard/joint_cmd_safe"
    assert bridge_inputs["gripper_cmd"]["source"] == "budget-guard/gripper_cmd_safe"
    assert bridge_inputs["base_cmd"]["source"] == "budget-guard/base_cmd_safe"
    assert guard_inputs["joint_cmd"]["source"] == "script-s1-runtime/joint_cmd"
    assert guard_inputs["gripper_cmd"]["source"] == "script-s1-runtime/gripper_cmd"
    assert guard_inputs["base_cmd"]["source"] == "waypoint-nav/base_cmd"
    assert nodes["waypoint-nav"]["inputs"]["nav_goal"]["source"] == ("script-s1-runtime/nav_goal")


def test_script_wrapper_reuses_frozen_s1_reset_and_verifier_sources():
    """CON-7: script and expert S1 arms share the exact reset and verifier sources."""
    expert = _graph_nodes(REPO_ROOT / "graphs" / "expert_s1.yaml")
    wrapper = _graph_nodes(REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml")

    assert wrapper["reset"]["path"] == expert["reset"]["path"]
    assert wrapper["verifier-retail"]["path"] == expert["verifier-retail"]["path"]


def test_script_wrapper_has_one_policy_behavioral_node():
    """CON-7: the one candidate-controlled behavior surface is the fixed script runtime."""
    expert = _graph_nodes(REPO_ROOT / "graphs" / "expert_s1.yaml")
    wrapper = _graph_nodes(REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml")
    fixed_ids = {
        "dora-genesis",
        "reset",
        "budget-guard",
        "waypoint-nav",
        "verifier-retail",
        "rollout-client",
    }

    assert set(wrapper) == fixed_ids | {"script-s1-runtime"}
    assert all(wrapper[node_id]["path"] == expert[node_id]["path"] for node_id in fixed_ids)
    assert wrapper["script-s1-runtime"]["path"] == "../src/aisle/nodes/script_s1_runtime.py"


def test_script_wrapper_exposes_policy_events_without_raw_node_traces():
    """CON-7: script agents receive policy_event diagnostics, not AISLE per-node traces."""
    nodes = _graph_nodes(REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml")

    assert "policy_event" in nodes["script-s1-runtime"]["outputs"]
    assert "trace-recorder" not in nodes
    assert all("AISLE_TRACE_DIR" not in (node.get("env") or {}) for node in nodes.values())


def test_script_runtime_normalizes_dora_inputs_as_policy_events():
    """CON-5: fixed translation gives script policies deterministic JSON observations."""
    import pyarrow as pa

    from aisle.nodes.script_s1_runtime import policy_event_from_dora

    goal = policy_event_from_dora(
        "episode_goal",
        pa.array(['{"order":[{"product":"ibuprofen","qty":1}]}']),
        {"sim_time_ns": 12},
    )
    state = policy_event_from_dora(
        "joint_state",
        pa.array([0.25, -0.5]),
        {"sim_time_ns": 34},
    )

    assert goal.kind == "episode_goal"
    assert goal.payload == {"order": [{"product": "ibuprofen", "qty": 1}]}
    assert goal.sim_time_ns == 12
    assert state.kind == "joint_state"
    assert state.payload == {"values": [0.25, -0.5]}
    assert state.sim_time_ns == 34


def test_script_runtime_preserves_well_shaped_unsafe_commands_for_the_guard():
    """BG-1..3: runtime preserves unsafe requests so the external guard clamps them."""
    from aisle.nodes.script_s1_runtime import prepare_commands
    from baselines.script_s1.contract import PolicyCommand

    unsafe_joint_request = [99.0] * 9
    commands = prepare_commands(
        [
            PolicyCommand("joint_cmd", unsafe_joint_request),
            PolicyCommand("gripper_cmd", [99.0]),
        ]
    )

    assert commands[0].kind == "joint_cmd"
    assert commands[0].payload == unsafe_joint_request
    assert commands[1].kind == "gripper_cmd"
    assert commands[1].payload == [99.0]


def test_script_runtime_translates_declared_semantic_gripper_actions():
    """BG-1: documented open/close commands serialize to the guarded scalar channel."""
    from aisle.nodes.script_s1_runtime import prepare_commands
    from baselines.script_s1.contract import PolicyCommand

    commands = prepare_commands(
        [
            PolicyCommand("gripper_cmd", {"action": "open"}),
            PolicyCommand("gripper_cmd", {"action": "close"}),
        ]
    )

    assert [command.payload for command in commands] == [[0.0], [1.0]]


def test_script_runtime_rejects_all_commands_before_any_can_be_emitted():
    """BG-1, CON-8: malformed command batches terminate atomically as COMMAND_INVALID."""
    from aisle.nodes.script_s1_runtime import CommandInvalid, prepare_commands
    from baselines.script_s1.contract import PolicyCommand

    commands = [
        PolicyCommand("joint_cmd", [0.0] * 9),
        PolicyCommand("joint_cmd", [0.0] * 8),
    ]

    with pytest.raises(CommandInvalid, match="COMMAND_INVALID"):
        prepare_commands(commands)


def test_run_script_rollout_launches_fixed_wrapper_and_returns_attempt_result(
    tmp_path: Path, monkeypatch
):
    """HAR-1, BG-1: rollout launches only the fixed wrapper and parses neutral results."""
    from aisle.harness import script_rollout

    graph_dir = tmp_path / "graphs"
    graph_dir.mkdir()
    (graph_dir / "ablation_script_s1_wrapper.yaml").write_text(
        (REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml").read_text()
    )
    policy = tmp_path / "candidate.py"
    policy.write_text("def create_policy(seed):\n    return object()\n")
    captured: dict[str, object] = {}

    class CompletedGraph:
        pid = 999_999_999

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def launch(graph: Path, run_dir: Path, env: dict, stderr):
        captured.update(graph=graph, run_dir=run_dir, env=env)
        Path(env["AISLE_RESULTS"]).write_text(
            '{"episode":0,"seed":3,"status":"fail","failure":"timeout","t_end":4.5}\n'
        )
        return CompletedGraph()

    monkeypatch.setattr(script_rollout, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)
    monkeypatch.setattr(script_rollout, "_terminate_script", lambda proc: None)
    monkeypatch.setattr(script_rollout, "reap_orphans", lambda run_dir: None)

    result = script_rollout.run_script_rollout(policy, "3", "script-unit")

    assert result.attempt_id == "script-unit"
    assert result.episodes == (
        {"episode": 0, "seed": 3, "status": "fail", "failure": "timeout", "t_end": 4.5},
    )
    assert result.failures == {"timeout": 1}
    assert result.safety.to_dict() == {"ungated": 0, "clamps": 0, "extra_item": 0}
    assert Path(captured["graph"]).parent == Path(captured["run_dir"])
    env = captured["env"]
    assert env["AISLE_SCRIPT_POLICY"] == str(policy.resolve())
    assert env["AISLE_SEED"] == "3"
    assert env["AISLE_SEEDS"] == "3"
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}:{tmp_path}"
    assert "traces_dir" not in result.artifacts


def test_run_script_rollout_refuses_unsafe_run_ids(tmp_path: Path):
    """CON-8: a traversal-shaped script run id cannot escape the external run store."""
    from aisle.harness.script_rollout import run_script_rollout

    policy = tmp_path / "candidate.py"
    policy.write_text("def create_policy(seed):\n    return object()\n")

    with pytest.raises(ValueError, match="unsafe run_id"):
        run_script_rollout(policy, "0", "../escape")


def test_script_runtime_is_in_the_fixed_orphan_reaper():
    """CON-7: script rollout cleanup includes its fixed candidate-hosting node."""
    from aisle.harness.reaper import NODE_PATTERNS

    assert "nodes/script_s1_runtime.py" in NODE_PATTERNS
