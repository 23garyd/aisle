"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from dataclasses import replace
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


def test_attempt_result_reserves_zero_hash_for_absent_candidate_refusals_only():
    """CON-5, CON-8: absent candidates have one explicit identity; byte hashes never alias it."""
    from aisle.harness.ablation import AttemptResult

    absent = {
        **_attempt(),
        "candidate_hash": "0" * 64,
        "failures": {"INFRA_ARGUMENT": 1},
        "artifacts": {"candidate_identity": "absent"},
    }
    assert AttemptResult.from_dict(absent).to_dict() == absent
    assert (
        AttemptResult.from_dict(
            {**absent, "artifacts": {"candidate_identity": "absent", "request": "missing"}}
        ).artifacts["request"]
        == "missing"
    )

    with pytest.raises(ValueError, match="reserved"):
        AttemptResult.from_dict({**absent, "artifacts": {}})
    with pytest.raises(ValueError, match="reserved"):
        AttemptResult.from_dict({**absent, "failures": {"INFRA_PROTOCOL": 1}})
    with pytest.raises(ValueError, match="candidate_identity"):
        AttemptResult.from_dict({**absent, "candidate_hash": "a" * 64})


@pytest.mark.parametrize(
    ("candidate_hash", "failures", "artifacts"),
    [
        ("0" * 64, {"timeout": 1}, {"candidate_identity": "absent"}),
        ("0" * 64, {"INFRA_ARGUMENT": 1}, {}),
        ("a" * 64, {"INFRA_ARGUMENT": 1}, {"candidate_identity": "absent"}),
    ],
)
def test_attempt_result_constructor_enforces_reserved_candidate_identity(
    candidate_hash: str, failures: dict[str, int], artifacts: dict[str, str]
):
    """CON-5: direct attempt construction cannot bypass the reserved hash identity rule."""
    from aisle.harness.ablation import AttemptResult, PreflightResult, SafetyResult

    with pytest.raises(ValueError, match="candidate"):
        AttemptResult(
            attempt_id="A-0001",
            candidate_hash=candidate_hash,
            preflight=PreflightResult(ok=False, errors=(), wall_s=0.0),
            episodes=(),
            failures=failures,
            safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
            timing={"wall_s": 0.0, "sim_s": 0.0},
            artifacts=artifacts,
        )


def test_attempt_result_serialization_rejects_mutated_zero_hash_failures():
    """CON-5: serialization revalidates a sentinel identity after failures mutate."""
    from aisle.harness.ablation import AttemptResult

    raw = {
        **_attempt(),
        "candidate_hash": "0" * 64,
        "failures": {"INFRA_ARGUMENT": 1},
        "artifacts": {"candidate_identity": "absent"},
    }
    result = AttemptResult.from_dict(raw)
    result.failures["timeout"] = 1

    with pytest.raises(ValueError, match="reserved"):
        result.to_dict()


def test_attempt_result_serialization_rejects_mutated_nonzero_hash_identity():
    """CON-5: serialization revalidates a byte hash after artifacts mutate."""
    from aisle.harness.ablation import AttemptResult

    result = AttemptResult.from_dict(_attempt())
    result.artifacts["candidate_identity"] = "invalid"

    with pytest.raises(ValueError, match="candidate_identity"):
        result.to_dict()


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


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("joint_cmd", [0.0] * 8 + ["1.0"]),
        ("joint_cmd", [0.0] * 8 + [float("nan")]),
        ("joint_cmd", [0.0] * 8 + [float("inf")]),
        ("joint_cmd", [0.0] * 8 + [1e39]),
        ("gripper_cmd", ["1.0"]),
        ("nav_goal", {"pose": [0.0, object(), 0.0]}),
        ("nav_goal", {1: "counter"}),
    ],
)
def test_script_runtime_deep_rejects_forged_or_mutated_payloads(kind: str, payload: object):
    """BG-1, CON-8: runtime distrusts forged PolicyCommand payload internals."""
    from aisle.nodes.script_s1_runtime import CommandInvalid, prepare_commands
    from baselines.script_s1.contract import PolicyCommand

    command = object.__new__(PolicyCommand)
    object.__setattr__(command, "kind", kind)
    object.__setattr__(command, "payload", payload)

    with pytest.raises(CommandInvalid, match="COMMAND_INVALID"):
        prepare_commands([command])


def test_script_runtime_emits_nothing_when_a_later_command_is_invalid():
    """BG-1, CON-8: the whole batch is wire-ready before its first emission."""
    from aisle.nodes.script_s1_runtime import CommandInvalid, emit_policy_commands
    from baselines.script_s1.contract import PolicyCommand

    invalid = object.__new__(PolicyCommand)
    object.__setattr__(invalid, "kind", "gripper_cmd")
    object.__setattr__(invalid, "payload", ["1.0"])
    emitted: list[tuple] = []

    with pytest.raises(CommandInvalid, match="COMMAND_INVALID"):
        emit_policy_commands(
            [PolicyCommand("joint_cmd", [0.0] * 9), invalid],
            lambda *args: emitted.append(args),
            {"sim_time_ns": 10, "env_id": 0},
            nav_seq=0,
        )

    assert emitted == []


def test_script_runtime_rejects_equality_spoofing_kind_before_any_emission():
    """BG-1, CON-8: command dispatch accepts only exact built-in string kinds."""
    from aisle.nodes.script_s1_runtime import CommandInvalid, emit_policy_commands
    from baselines.script_s1.contract import PolicyCommand

    class SpoofKind:
        def __eq__(self, other):
            return True

    command = object.__new__(PolicyCommand)
    object.__setattr__(command, "kind", SpoofKind())
    object.__setattr__(command, "payload", {"target": [0.0, 0.0, 0.0]})
    emitted: list[tuple] = []

    with pytest.raises(CommandInvalid, match="COMMAND_INVALID"):
        emit_policy_commands(
            [command],
            lambda *args: emitted.append(args),
            {"sim_time_ns": 10, "env_id": 0},
            nav_seq=0,
        )

    assert emitted == []


def _episode_record(episode: int, seed: int) -> dict:
    return {
        "episode": episode,
        "seed": seed,
        "status": "fail",
        "failure": "timeout",
        "t_end": 4.5,
        "success": False,
        "penalties": ["timeout"],
        "placement_scores": [],
        "goal_id": f"ep-{episode:04d}",
        "verifier": "oracle",
        "suite": "retail",
    }


@pytest.mark.parametrize(
    "record",
    [
        {"x": 1},
        {**_episode_record(0, 3), "episode": "0"},
        {**_episode_record(0, 3), "seed": "3"},
        {**_episode_record(0, 3), "status": "ongoing"},
        {**_episode_record(0, 3), "success": True},
        {**_episode_record(0, 3), "t_end": float("nan")},
        {**_episode_record(0, 3), "t_end": 10**400},
        {**_episode_record(0, 3), "penalties": [1]},
        {**_episode_record(0, 3), "failure": "extra_item"},
        {**_episode_record(0, 3), "penalties": ["invented"]},
        {**_episode_record(0, 3), "placement_scores": [1]},
        {
            **_episode_record(0, 3),
            "placement_scores": [
                {
                    "item": "item-1",
                    "slot": "A1-L0-S0",
                    "pos": "true",
                    "yaw": True,
                    "front_face": True,
                    "overhang": True,
                    "alignment": True,
                }
            ],
        },
        {**_episode_record(0, 3), "goal_id": "forged"},
        {**_episode_record(0, 3), "verifier": "realistic"},
        {**_episode_record(0, 3), "verifier": "candidate"},
        {**_episode_record(0, 3), "suite": "desk"},
    ],
)
def test_script_rollout_rejects_malformed_episode_records(tmp_path: Path, record: dict):
    """HAR-1, CON-8: arbitrary or ill-typed JSON cannot count as an episode."""
    from aisle.harness.script_rollout import _read_episode_results

    path = tmp_path / "episodes.jsonl"
    path.write_text(json.dumps(record) + "\n")

    assert _read_episode_results(path, [3]) == ([], 1)


def test_script_rollout_rejects_wrong_seed_and_episode_order(tmp_path: Path):
    """HAR-1, CON-5: only the requested seed sequence can complete a rollout."""
    from aisle.harness.script_rollout import _read_episode_results

    path = tmp_path / "episodes.jsonl"
    path.write_text(
        "\n".join(
            (
                json.dumps(_episode_record(1, 4)),
                json.dumps(_episode_record(0, 99)),
                json.dumps(_episode_record(0, 3)),
                json.dumps(_episode_record(1, 4)),
            )
        )
        + "\n"
    )

    assert _read_episode_results(path, [3, 4]) == (
        [_episode_record(0, 3), _episode_record(1, 4)],
        2,
    )


def test_script_rollout_waits_after_forced_sigkill(monkeypatch):
    """CON-8: forced script graph cleanup reaps the killed child process."""
    from aisle.harness import script_rollout

    signals: list[int] = []

    class StuckProcess:
        pid = 12345

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise script_rollout.subprocess.TimeoutExpired("dora", timeout)
            return -signal.SIGKILL

    process = StuckProcess()
    monkeypatch.setattr(
        script_rollout.os,
        "killpg",
        lambda pid, sent_signal: signals.append(sent_signal),
    )

    script_rollout._terminate_script(process)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waits == 2


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
        Path(env["AISLE_RESULTS"]).write_text(json.dumps(_episode_record(0, 3)) + "\n")
        return CompletedGraph()

    monkeypatch.setattr(script_rollout, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)
    monkeypatch.setattr(script_rollout, "_terminate_script", lambda proc: None)
    monkeypatch.setattr(script_rollout, "reap_orphans", lambda run_dir: None)

    import pyarrow as pa

    from aisle.harness.trace_recorder import TRACE_SCHEMA

    def launch(graph: Path, run_dir: Path, env: dict, stderr):
        captured.update(graph=graph, run_dir=run_dir, env=env)
        Path(env["AISLE_RESULTS"]).write_text(json.dumps(_episode_record(0, 3)) + "\n")
        traces = run_dir / "traces"
        with pa.ipc.new_stream(traces / "budget-guard__violation.arrow", TRACE_SCHEMA) as writer:
            writer.write_batch(
                pa.record_batch(
                    [
                        pa.array([0], pa.int64()),
                        pa.array([0], pa.int32()),
                        pa.array([0], pa.int64()),
                        pa.array([None], pa.list_(pa.float64())),
                        pa.array(['{"reason":"shutdown_tail"}'], pa.string()),
                    ],
                    schema=TRACE_SCHEMA,
                )
            )
        return CompletedGraph()

    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)

    result = script_rollout.run_script_rollout(policy, "3", "script-unit")

    assert result.attempt_id == "script-unit"
    assert result.episodes == (_episode_record(0, 3),)
    assert result.failures == {"timeout": 1}
    assert result.safety.to_dict() == {"ungated": 0, "clamps": 1, "extra_item": 0}
    assert Path(captured["graph"]).parent == Path(captured["run_dir"])
    env = captured["env"]
    assert env["AISLE_SCRIPT_POLICY"] == str(policy.resolve())
    assert env["AISLE_SEED"] == "3"
    assert env["AISLE_SEEDS"] == "3"
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}:{tmp_path}"
    assert "traces_dir" not in result.artifacts


@pytest.mark.parametrize("evidence", ["missing", "textless", "malformed", "truncated"])
def test_run_script_rollout_fails_closed_when_guard_trace_is_incomplete(
    tmp_path: Path, monkeypatch, evidence: str
):
    """CON-5, CON-7: protected script rollouts require the complete guard violation stream."""
    import pyarrow as pa

    from aisle.harness import script_rollout
    from aisle.harness.trace_recorder import TRACE_SCHEMA

    (tmp_path / "graphs").mkdir()
    (tmp_path / "graphs" / "ablation_script_s1_wrapper.yaml").write_text(
        (REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml").read_text()
    )
    policy = tmp_path / "candidate.py"
    policy.write_text("def create_policy(seed):\n    return object()\n")

    class CompletedGraph:
        pid = 999_999_999

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def launch(graph: Path, run_dir: Path, env: dict, stderr):
        Path(env["AISLE_RESULTS"]).write_text(json.dumps(_episode_record(0, 3)) + "\n")
        path = run_dir / "traces" / "budget-guard__violation.arrow"
        if evidence == "textless":
            with pa.ipc.new_stream(path, pa.schema([("wrong", pa.string())])) as writer:
                writer.write_batch(pa.record_batch([pa.array(["ignored"])], names=["wrong"]))
        elif evidence != "missing":
            with pa.ipc.new_stream(path, TRACE_SCHEMA) as writer:
                writer.write_batch(
                    pa.record_batch(
                        [
                            pa.array([0], pa.int64()),
                            pa.array([0], pa.int32()),
                            pa.array([0], pa.int64()),
                            pa.array([None], pa.list_(pa.float64())),
                            pa.array(['{"reason":"shutdown_tail"}'], pa.string()),
                        ],
                        schema=TRACE_SCHEMA,
                    )
                )
            if evidence == "malformed":
                path.write_bytes(b"not-arrow")
            elif evidence == "truncated":
                path.write_bytes(path.read_bytes()[:-8])
        return CompletedGraph()

    monkeypatch.setattr(script_rollout, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)
    monkeypatch.setattr(script_rollout, "_terminate_script", lambda proc: None)
    monkeypatch.setattr(script_rollout, "reap_orphans", lambda run_dir: None)

    result = script_rollout.run_script_rollout(policy, "3", f"script-{evidence}")

    assert result.failures == {"timeout": 1, "INFRA_SAFETY_UNAVAILABLE": 1}


def test_candidate_identity_distinguishes_missing_and_invalid_candidates(
    tmp_path: Path, monkeypatch
):
    """CON-5, CON-8: directories and unreadable candidates never alias a missing file."""
    from aisle.harness import ablation_adapters
    from aisle.harness.ablation_adapters import ScriptAdapter

    directory = tmp_path / "candidate-dir"
    directory.mkdir()
    assert ScriptAdapter().rollout(directory, "3", "directory").artifacts == {
        "candidate_identity": "invalid"
    }

    policy = tmp_path / "candidate.py"
    policy.write_text("def create_policy(seed):\n    return object()\n")
    monkeypatch.setattr(
        ablation_adapters,
        "sha256_file",
        lambda path: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert ScriptAdapter().rollout(policy, "3", "unreadable").artifacts == {
        "candidate_identity": "invalid"
    }


def test_candidate_identity_rejects_reserved_zero_from_a_real_file(tmp_path: Path, monkeypatch):
    """CON-5: a real candidate hash can never use the missing-input sentinel."""
    from aisle.harness import ablation_adapters
    from aisle.harness.ablation_adapters import ScriptAdapter

    policy = tmp_path / "candidate.py"
    policy.write_text("def create_policy(seed):\n    return object()\n")
    monkeypatch.setattr(ablation_adapters, "sha256_file", lambda path: "0" * 64)

    result = ScriptAdapter().rollout(policy, "3", "zero-hash")

    assert result.failures == {"INFRA_ARGUMENT": 1}
    assert result.artifacts == {"candidate_identity": "invalid"}


@pytest.mark.parametrize("resolve_error_type", [PermissionError, OSError, FileNotFoundError])
def test_adapters_map_candidate_resolution_errors_to_invalid_identity(
    tmp_path: Path, resolve_error_type: type[OSError]
):
    """CON-5, CON-8: resolver failures are invalid input, never an absent-file identity."""
    from aisle.harness.ablation_adapters import AisleAdapter, ScriptAdapter

    class ResolveFailurePath(type(Path())):
        def resolve(self, strict: bool = False) -> Path:
            raise resolve_error_type("candidate resolution failed")

    candidate = ResolveFailurePath(tmp_path / "candidate")

    def unexpected_call(*args, **kwargs):
        raise AssertionError("invalid candidates must not reach an external runner")

    adapters = (
        AisleAdapter(tmp_path, runner=unexpected_call),
        ScriptAdapter(
            preflight_runner=unexpected_call,
            rollout_runner=unexpected_call,
        ),
    )

    for adapter in adapters:
        result = adapter.rollout(candidate, "3", "resolve-error")
        assert result.candidate_hash == "0" * 64
        assert result.failures == {"INFRA_ARGUMENT": 1}
        assert result.artifacts == {"candidate_identity": "invalid"}


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


def _adapter_rollout_report(run_id: str) -> dict:
    return {
        "ok": True,
        "run_id": run_id,
        "episodes": [_episode_record(0, 3)],
        "failures": {"timeout": 1},
        "safety": {"ungated": 0, "clamps": 2, "extra_item": 0},
        "durations": {"wall_s": 1.25, "sim_s": 4.5},
        "traces_dir": "runs/unit/traces",
        "videos": ["runs/unit/traces/overhead.mp4"],
    }


def test_aisle_adapter_uses_fixed_harness_argv_and_normalizes_result(tmp_path: Path):
    """HAR-1, CON-5, CON-8: AISLE invokes validate/rollout through one neutral record boundary."""
    from aisle.harness.ablation_adapters import AisleAdapter

    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("nodes: []\n")
    calls: list[tuple[list[str], float]] = []

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        calls.append((argv, timeout))
        if argv[1] == "validate":
            return subprocess.CompletedProcess(
                argv, 0, '{"ok":true,"errors":[],"warnings":[]}\n', ""
            )
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_adapter_rollout_report("A-01")) + "\n", ""
        )

    adapter = AisleAdapter(tmp_path, runner=run)

    preflight = adapter.preflight(candidate)
    result = adapter.rollout(candidate, "3", "A-01")

    assert preflight.ok is True
    assert [timeout for _, timeout in calls] == [30.0, 2520.0]
    assert [argv for argv, _ in calls] == [
        [
            "harness",
            "validate",
            str(candidate.resolve()),
            "--root",
            str(tmp_path.resolve()),
            "--embodiment",
            "mobile",
        ],
        [
            "harness",
            "rollout",
            "--graph",
            str(candidate.resolve()),
            "--tier",
            "S1",
            "--embodiment",
            "mobile",
            "--episodes",
            "1",
            "--seeds",
            "3",
            "--reset",
            "teleport",
            "--verifier",
            "oracle",
            "--root",
            str(tmp_path.resolve()),
            "--no-idea-gate",
            "--run-id",
            "A-01",
        ],
    ]
    assert result.attempt_id == "A-01"
    assert (
        result.candidate_hash == "704056ab12a52a1a941fd543cb0207a1117d3fb8884a0428a7c7ccce5feb946e"
    )
    assert result.failures == {"timeout": 1}
    assert result.safety.to_dict() == {"ungated": 0, "clamps": 2, "extra_item": 0}
    assert result.timing == {"wall_s": 1.25, "sim_s": 4.5}
    assert result.artifacts == {
        "traces_dir": "runs/unit/traces",
        "video_0": "runs/unit/traces/overhead.mp4",
    }


def test_script_adapter_uses_preflight_and_protected_runner_without_harness_validation(
    tmp_path: Path,
):
    """HAR-1, CON-7, CON-8: script candidates use the isolated gate and protected runner."""
    from aisle.harness.ablation import AttemptResult, PreflightResult, SafetyResult
    from aisle.harness.ablation_adapters import ScriptAdapter

    candidate = tmp_path / "candidate.py"
    candidate.write_text("def create_policy(seed):\n    return object()\n")
    preflight_calls: list[Path] = []
    rollout_calls: list[tuple[Path, str, str]] = []

    def preflight(path: Path) -> PreflightResult:
        preflight_calls.append(path)
        return PreflightResult(ok=True, errors=(), wall_s=0.25)

    def rollout(path: Path, seeds: str, run_id: str) -> AttemptResult:
        rollout_calls.append((path, seeds, run_id))
        return AttemptResult(
            attempt_id=run_id,
            candidate_hash="b" * 64,
            preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
            episodes=(_episode_record(0, 3),),
            failures={"timeout": 1},
            safety=SafetyResult(ungated=0, clamps=2, extra_item=0),
            timing={"wall_s": 1.25, "sim_s": 4.5},
            artifacts={
                "policy_log": "runs/unit/policy_events.jsonl",
                "raw_trace": "runs/unit/traces/secret.arrow",
            },
        )

    adapter = ScriptAdapter(preflight_runner=preflight, rollout_runner=rollout)
    result = adapter.rollout(candidate, "3", "A-01")

    assert preflight_calls == [candidate]
    assert rollout_calls == [(candidate, "3", "A-01")]
    assert (
        result.candidate_hash == "7396a812496952c23c81b50f59a1c9f4b51b987c0da3a34b2c2783ea30c03d3e"
    )
    assert result.preflight.to_dict() == {"ok": True, "errors": [], "wall_s": 0.25}
    assert result.failures == {"timeout": 1}
    assert result.safety.to_dict() == {"ungated": 0, "clamps": 2, "extra_item": 0}
    assert result.timing == {"wall_s": 1.25, "sim_s": 4.5}
    assert adapter.collect_deliverable(candidate) == {"policy_log": "runs/unit/policy_events.jsonl"}


@pytest.mark.parametrize(
    ("stdout", "error_code"),
    [
        ('{"ok":true}{"ok":true}', "INFRA_PROTOCOL"),
        ("not-json", "INFRA_PROTOCOL"),
    ],
)
def test_aisle_adapter_fails_closed_on_non_single_json_stdout(
    tmp_path: Path, stdout: str, error_code: str
):
    """CON-8: adapter subprocess responses must contain exactly one JSON object."""
    from aisle.harness.ablation_adapters import AisleAdapter

    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("nodes: []\n")

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    result = AisleAdapter(tmp_path, runner=run).preflight(candidate)

    assert result.ok is False
    assert result.errors[0]["code"] == error_code


def test_adapters_map_subprocess_timeouts_to_the_same_stable_failure(tmp_path: Path):
    """CON-5, CON-8: external command timeouts normalize to deterministic infrastructure records."""
    from aisle.harness.ablation_adapters import AisleAdapter

    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("nodes: []\n")

    def timeout(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(argv, timeout_s)

    result = AisleAdapter(tmp_path, runner=timeout).rollout(candidate, "3", "A-01")

    assert result.preflight.errors[0]["code"] == "INFRA_TIMEOUT"
    assert result.failures == {"PREFLIGHT_FAILED": 1}


def test_aisle_adapter_fails_closed_when_rollout_omits_guard_evidence(tmp_path: Path):
    """CON-5, CON-7: unavailable AISLE guard evidence must not be recorded as observed zero."""
    from aisle.harness.ablation_adapters import AisleAdapter

    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("nodes: []\n")
    report = _adapter_rollout_report("A-01")
    report["safety"] = None

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        stdout = '{"ok":true,"errors":[],"warnings":[]}\n'
        if argv[1] == "rollout":
            stdout = json.dumps(report) + "\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    adapter = AisleAdapter(tmp_path, runner=run)
    adapter.preflight(candidate)
    result = adapter.rollout(candidate, "3", "A-01")

    assert result.episodes == (_episode_record(0, 3),)
    assert result.failures == {"timeout": 1, "INFRA_SAFETY_UNAVAILABLE": 1}
    assert result.timing == {"wall_s": 1.25, "sim_s": 4.5}
    assert result.artifacts == {
        "traces_dir": "runs/unit/traces",
        "video_0": "runs/unit/traces/overhead.mp4",
    }


def test_adapters_preserve_equivalent_records_when_guard_evidence_is_unavailable(
    tmp_path: Path, monkeypatch
):
    """CON-5, CON-7: real script and CLI paths retain identical unavailable-safety records."""
    from aisle.harness import script_rollout
    from aisle.harness.ablation import PreflightResult
    from aisle.harness.ablation_adapters import AisleAdapter, ScriptAdapter

    (tmp_path / "graphs").mkdir()
    (tmp_path / "graphs" / "ablation_script_s1_wrapper.yaml").write_text(
        (REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml").read_text()
    )
    aisle_candidate = tmp_path / "candidate.yaml"
    aisle_candidate.write_text("nodes: []\n")
    script_candidate = tmp_path / "candidate.py"
    script_candidate.write_text("def create_policy(seed):\n    return object()\n")
    episode = _episode_record(0, 3)

    class CompletedGraph:
        pid = 999_999_999

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def launch(graph: Path, run_dir: Path, env: dict, stderr):
        Path(env["AISLE_RESULTS"]).write_text(json.dumps(episode) + "\n")
        return CompletedGraph()

    def aisle_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        if argv[1] == "validate":
            response = {"ok": True, "errors": [], "warnings": []}
        else:
            response = {
                **_adapter_rollout_report("unavailable-parity"),
                "safety": None,
                "durations": {"wall_s": 0.0, "sim_s": 4.5},
            }
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    monkeypatch.setattr(script_rollout, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)
    monkeypatch.setattr(script_rollout, "_terminate_script", lambda proc: None)
    monkeypatch.setattr(script_rollout, "reap_orphans", lambda run_dir: None)
    monkeypatch.setattr(script_rollout.time, "monotonic", lambda: 10.0)

    aisle = AisleAdapter(tmp_path, runner=aisle_runner).rollout(
        aisle_candidate, "3", "unavailable-parity"
    )
    script = ScriptAdapter(
        preflight_runner=lambda path: PreflightResult(ok=True, errors=(), wall_s=0.0)
    ).rollout(script_candidate, "3", "unavailable-parity")

    comparable_fields = ("attempt_id", "preflight", "episodes", "failures", "safety", "timing")
    aisle_record = aisle.to_dict()
    script_record = script.to_dict()
    assert (
        {field: aisle_record[field] for field in comparable_fields}
        == {field: script_record[field] for field in comparable_fields}
        == {
            "attempt_id": "unavailable-parity",
            "preflight": {"ok": True, "errors": [], "wall_s": 0.0},
            "episodes": [episode],
            "failures": {"timeout": 1, "INFRA_SAFETY_UNAVAILABLE": 1},
            "safety": {"ungated": 0, "clamps": 0, "extra_item": 0},
            "timing": {"wall_s": 0.0, "sim_s": 4.5},
        }
    )
    assert aisle_record["candidate_hash"] != script_record["candidate_hash"]
    assert aisle_record["artifacts"] != script_record["artifacts"]


@pytest.mark.parametrize(("seeds", "run_id"), [("bad", "A-01"), ("3", "../escape")])
def test_script_adapter_maps_invalid_rollout_arguments_without_launching_runner(
    tmp_path: Path, seeds: str, run_id: str
):
    """CON-8: script and AISLE adapters return stable INFRA_ARGUMENT records for invalid input."""
    from aisle.harness.ablation import PreflightResult
    from aisle.harness.ablation_adapters import ScriptAdapter

    candidate = tmp_path / "candidate.py"
    candidate.write_text("def create_policy(seed):\n    return object()\n")
    launches: list[tuple[Path, str, str]] = []
    adapter = ScriptAdapter(
        preflight_runner=lambda path: PreflightResult(ok=True, errors=(), wall_s=0.1),
        rollout_runner=lambda path, requested_seeds, requested_run_id: launches.append(
            (path, requested_seeds, requested_run_id)
        ),
    )

    result = adapter.rollout(candidate, seeds, run_id)

    assert result.failures == {"INFRA_ARGUMENT": 1}
    assert launches == []


def test_script_adapter_maps_missing_candidate_to_stable_argument_failure(tmp_path: Path):
    """CON-8: an absent script policy cannot escape as FileNotFoundError."""
    from aisle.harness.ablation_adapters import ScriptAdapter

    candidate = tmp_path / "missing.py"
    result = ScriptAdapter().rollout(candidate, "3", "A-01")

    assert result.failures == {"INFRA_ARGUMENT": 1}
    assert result.candidate_hash == "0" * 64
    assert result.artifacts == {"candidate_identity": "absent"}


def test_script_adapter_preflight_failure_measures_elapsed_wall_time(tmp_path: Path):
    """CON-5, CON-8: script preflight failures use the measured wall-time record."""
    from aisle.harness.ablation_adapters import ScriptAdapter

    candidate = tmp_path / "candidate.py"
    candidate.write_text("def create_policy(seed):\n    return object()\n")
    ticks = iter((10.0, 10.75))

    def timeout(path: Path):
        raise subprocess.TimeoutExpired(["preflight"], 10)

    result = ScriptAdapter(preflight_runner=timeout, clock=lambda: next(ticks)).preflight(candidate)

    assert result.to_dict() == {"ok": False, "errors": [{"code": "INFRA_TIMEOUT"}], "wall_s": 0.75}


def _controller_runtime(
    module,
    *,
    agent_lines: tuple[str, ...] | None = None,
    adapter_factory=None,
    frozen_drift: tuple[str, ...] = (),
):
    """Fixture-only controller boundaries: no agent, simulator, or environment sync."""
    lines = agent_lines
    if lines is None:
        lines = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 3,
                            "cache_creation_input_tokens": 2,
                            "output_tokens": 1,
                        }
                    },
                }
            )
            + "\n",
        )

    def create_worktree(pin: str, destination: Path) -> Path:
        assert pin == "a" * 40
        for relative in (
            "graphs/ablation_s1_starter.yaml",
            "baselines/script_s1/starter.py",
            "harness/budget.toml",
        ):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, target)
        return destination

    def execute(
        command,
        cwd,
        agent,
        on_line,
        stop_reason,
        wall_ceiling_s,
        environment,
    ):
        assert command and cwd.is_dir() and agent in {"claude", "codex"}
        assert wall_ceiling_s <= 4 * 3600
        assert environment["AISLE_ABLATION_SESSION"]
        assert environment["AISLE_ABLATION_CONTROLLER"].endswith("tools/s1_harness_ablation.py")
        try:
            for line in lines:
                on_line(line, 0.25)
                reason = stop_reason(0.25)
                if reason:
                    return module.ExecutionResult(
                        stopped=reason,
                        returncode=-signal.SIGKILL,
                        wall_s=0.25,
                        agent_version="fixture-agent-1",
                    )
        except module.TelemetryError:
            return module.ExecutionResult(
                stopped="telemetry_invalid",
                returncode=-signal.SIGKILL,
                wall_s=0.25,
                agent_version="fixture-agent-1",
            )
        return module.ExecutionResult(
            stopped="agent_done",
            returncode=0,
            wall_s=0.25,
            agent_version="fixture-agent-1",
        )

    return module.ControllerRuntime(
        resolve_pin=lambda repo, pin: "a" * 40,
        create_worktree=create_worktree,
        execute_agent=execute,
        adapter_factory=adapter_factory or module.default_adapter_factory,
        audit_frozen=lambda worktree, pin: list(frozen_drift),
        worktree_head=lambda worktree: "a" * 40,
        worktree_status=lambda worktree: [],
        epoch_time=lambda: 1_800_000_000.0,
    )


def _invoke_controller(module, argv: list[str], runtime) -> tuple[int, dict, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "stderr", stderr)
        returncode = module.main(argv, runtime=runtime, repo_root=REPO_ROOT)
    output = stdout.getvalue()
    decoder = json.JSONDecoder()
    result, end = decoder.raw_decode(output)
    assert isinstance(result, dict)
    assert output[end:].strip() == ""
    assert output.endswith("\n")
    return returncode, result, output, stderr.getvalue()


def _prepare_controller_campaign(module, tmp_path: Path, *, pairs: int = 1, runtime=None):
    campaign = tmp_path / "campaign"
    runtime = runtime or _controller_runtime(module)
    returncode, result, _, _ = _invoke_controller(
        module,
        [
            "prepare",
            "--pin",
            "fixture-pin",
            "--pairs",
            str(pairs),
            "--assignment-seed",
            "17",
            "--out",
            str(campaign),
        ],
        runtime,
    )
    assert returncode == 0 and result["ok"] is True
    return campaign, result, runtime


def _run_prepared_controller_session(module, session_dir: Path, runtime):
    session = json.loads((session_dir / "session.json").read_text())
    return _invoke_controller(
        module,
        [
            "run",
            "--session",
            str(session_dir),
            "--condition",
            session["condition"],
            "--agent",
            "claude",
            "--model",
            "fixture-model",
            "--tokens",
            "500000",
            "--episodes",
            "40",
            "--wall-h",
            "4",
        ],
        runtime,
    )


def test_controller_prepare_is_deterministic_paired_and_copies_only_assigned_surface(
    tmp_path: Path,
):
    """CON-5, CON-7: preparation is paired, pinned, isolated, and outcome-blind."""
    from tools import s1_harness_ablation as controller

    first, first_result, runtime = _prepare_controller_campaign(
        controller, tmp_path / "first", pairs=3
    )
    second, second_result, _ = _prepare_controller_campaign(
        controller, tmp_path / "second", pairs=3, runtime=runtime
    )

    assert first_result["assignments"] == second_result["assignments"]
    assert len(first_result["assignments"]) == 6
    assert all(
        set(first_result["assignments"][index : index + 2]) == {"aisle", "script"}
        for index in range(0, 6, 2)
    )
    first_sessions = sorted(first.glob("S*"))
    second_sessions = sorted(second.glob("S*"))
    assert [(path / "session.json").read_bytes() for path in first_sessions] == [
        (path / "session.json").read_bytes() for path in second_sessions
    ]
    for session_dir in first_sessions:
        record = json.loads((session_dir / "session.json").read_text())
        assert "outcome" not in record and record["state"] == "prepared"
        assert record["pin"] == "a" * 40
        assert set(path.name for path in session_dir.iterdir() if path.is_file()) == {
            "session.json",
            "attempts.jsonl",
            "agent.jsonl",
            "token_samples.jsonl",
            "holdout.json",
            "audit.json",
        }
        aisle_candidate = session_dir / "worktree" / "graphs" / "agent_s1_ablation.yaml"
        script_candidate = session_dir / "worktree" / "baselines" / "script_s1" / "candidate.py"
        assert aisle_candidate.exists() == (record["condition"] == "aisle")
        assert script_candidate.exists() == (record["condition"] == "script")


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--tokens", "500001"),
        ("--episodes", "41"),
        ("--wall-h", "4.0001"),
        ("--wall-h", "nan"),
    ],
)
def test_controller_refuses_any_budget_above_the_frozen_ceiling(
    tmp_path: Path, option: str, value: str
):
    """CON-7, CON-8: an operator cannot raise token, episode, or wall ceilings."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    session = json.loads((session_dir / "session.json").read_text())
    argv = [
        "run",
        "--session",
        str(session_dir),
        "--condition",
        session["condition"],
        "--agent",
        "claude",
        "--model",
        "fixture-model",
        "--tokens",
        "500000",
        "--episodes",
        "40",
        "--wall-h",
        "4",
    ]
    argv[argv.index(option) + 1] = value

    returncode, response, _, _ = _invoke_controller(controller, argv, runtime)

    assert returncode == 1
    assert response["ok"] is False
    assert response["code"] == "BUDGET_LIMIT"
    assert json.loads((session_dir / "session.json").read_text())["state"] == "prepared"


def test_controller_condition_is_immutable_after_session_start(tmp_path: Path):
    """CON-5: assigned treatment cannot change after the external session starts."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    returncode, response, _, _ = _run_prepared_controller_session(controller, session_dir, runtime)
    assert returncode == 0 and response["ok"] is True
    assigned = json.loads((session_dir / "session.json").read_text())["condition"]

    returncode, response, _, _ = _invoke_controller(
        controller,
        [
            "run",
            "--session",
            str(session_dir),
            "--condition",
            "script" if assigned == "aisle" else "aisle",
            "--agent",
            "claude",
            "--model",
            "fixture-model",
            "--tokens",
            "500000",
            "--episodes",
            "40",
            "--wall-h",
            "4",
        ],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "CONDITION_IMMUTABLE"


def test_controller_refuses_heldout_scoring_before_agent_stop(tmp_path: Path):
    """CON-5, CON-8: held-out seeds remain external until the agent process stops."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "AGENT_NOT_STOPPED"
    assert json.loads((session_dir / "holdout.json").read_text()) == {
        "ok": False,
        "state": "pending",
    }


def test_controller_refuses_nonheldout_or_overlapping_score_seeds(tmp_path: Path):
    """CON-5, CON-8: development/regression seeds can never enter external scoring."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "0..7"],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "SEED_DOMAIN"


def test_controller_scores_no_deliverable_as_explicit_zero(tmp_path: Path):
    """CON-5: a stopped clean session without a deliverable has held-out pass@1 zero."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    record = json.loads((session_dir / "session.json").read_text())
    (session_dir / record["candidate"]).unlink()

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert returncode == 0
    assert response["ok"] is True
    assert response["outcome"] == "no_deliverable"
    holdout = json.loads((session_dir / "holdout.json").read_text())
    assert holdout["pass1"] == 0.0
    assert holdout["episodes"] == []
    assert holdout["failures"] == {}


def test_controller_malformed_live_agent_telemetry_fails_closed(tmp_path: Path):
    """HAR-5, CON-8: malformed live telemetry kills the agent and never implies zero spend."""
    from tools import s1_harness_ablation as controller

    runtime = _controller_runtime(controller, agent_lines=("not-json\n",))
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]

    returncode, response, _, _ = _run_prepared_controller_session(controller, session_dir, runtime)

    assert returncode == 1
    assert response["code"] == "TELEMETRY_INVALID"
    session = json.loads((session_dir / "session.json").read_text())
    assert session["state"] == "agent_stopped"
    assert session["telemetry"]["valid"] is False
    assert session["tokens_spent"] is None
    assert (session_dir / "token_samples.jsonl").read_text() == ""


def test_controller_live_stream_stops_at_token_budget_and_reserves_episode_budget(
    tmp_path: Path,
):
    """HAR-5, CON-7: live new-token spend and trusted rollout capacity are externally bounded."""
    from aisle.harness.rollout import budget_remaining
    from tools import s1_harness_ablation as controller

    line = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 499_998,
                        "cache_creation_input_tokens": 1,
                        "output_tokens": 1,
                    }
                },
            }
        )
        + "\n"
    )
    runtime = _controller_runtime(controller, agent_lines=(line,))
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]

    returncode, response, _, _ = _run_prepared_controller_session(controller, session_dir, runtime)

    assert returncode == 1
    assert response["code"] == "TOKEN_BUDGET_EXCEEDED"
    assert response["tokens_spent"] == 500_000
    assert budget_remaining(session_dir / "worktree")["episodes_left"] == 40


def test_controller_scores_unavailable_safety_evidence_fail_closed(tmp_path: Path):
    """CON-5, CON-7: INFRA_SAFETY_UNAVAILABLE is not inferred as held-out zero."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    class UnavailableSafetyAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
                episodes=(_episode_record(0, 100),),
                failures={"INFRA_SAFETY_UNAVAILABLE": 1},
                safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
                timing={"wall_s": 1.0, "sim_s": 1.0},
                artifacts={},
            )

    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: UnavailableSafetyAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "INFRA_SAFETY_UNAVAILABLE"
    assert response["pass1"] is None


def test_controller_scores_all_eight_heldout_seeds_and_clean_audit_passes(tmp_path: Path):
    """CON-5, CON-7: external scoring records all held-out outcomes and verified provenance."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    class HeldoutAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            assert seeds == "100..107"
            episodes = []
            for index, seed in enumerate(range(100, 108)):
                episode = _episode_record(index, seed)
                if index < 4:
                    episode.update(
                        status="success",
                        failure=None,
                        success=True,
                        penalties=[],
                    )
                episodes.append(episode)
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.1),
                episodes=tuple(episodes),
                failures={"timeout": 4},
                safety=SafetyResult(ungated=0, clamps=2, extra_item=0),
                timing={"wall_s": 8.0, "sim_s": 16.0},
                artifacts={},
            )

    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: HeldoutAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )
    assert returncode == 0
    assert response["pass1"] == 0.5

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )
    assert returncode == 0
    assert response["ok"] is True
    assert json.loads((session_dir / "audit.json").read_text())["issues"] == []


def test_controller_audit_marks_operator_frozen_and_concurrent_contamination(
    tmp_path: Path,
):
    """CON-7: operator intervention, frozen drift, and overlapping sessions are exclusions."""
    from tools import s1_harness_ablation as controller

    base_runtime = _controller_runtime(controller, frozen_drift=("env/limits.toml",))
    epochs = iter((10.0, 20.0, 15.0, 25.0))
    runtime = replace(base_runtime, epoch_time=lambda: next(epochs))
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    for session_name in result["sessions"]:
        _run_prepared_controller_session(controller, campaign / session_name, runtime)
    first = campaign / result["sessions"][0]
    first_record = json.loads((first / "session.json").read_text())
    first_record["operator_events"] = [{"kind": "hint", "authorized": False}]
    (first / "session.json").write_text(json.dumps(first_record))

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(campaign)],
        runtime,
    )

    assert returncode == 1
    first_audit = json.loads((first / "audit.json").read_text())
    assert {
        "OPERATOR_INTERVENTION",
        "FROZEN_DRIFT",
        "CONCURRENT_SIMULATOR",
    }.issubset(first_audit["issues"])
    assert response["ok"] is False


def test_controller_audit_detects_agent_ledger_tampering(tmp_path: Path):
    """CON-5, CON-7: audit fails a modified live-agent hash chain."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    ledger = session_dir / "agent.jsonl"
    ledger.write_text(ledger.read_text().replace('"input_tokens":3', '"input_tokens":4'))

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(campaign)],
        runtime,
    )

    assert returncode == 1
    assert response["ok"] is False
    audit = json.loads((session_dir / "audit.json").read_text())
    assert "AGENT_LEDGER_INVALID" in audit["issues"]


def test_controller_audit_rejects_validly_chained_early_budget_release(tmp_path: Path):
    """CON-7: semantic audit catches a forged settlement even when its chain is valid."""
    from aisle.harness.rollout import settle_budget
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    settle_budget(
        session_dir / "worktree",
        controller.CONTROLLER_RESERVATION,
        episodes=0,
        wall_s=0.0,
    )

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )

    assert returncode == 1
    assert response["ok"] is False
    audit = json.loads((session_dir / "audit.json").read_text())
    assert "BUDGET_AUTHORITY_TAMPER" in audit["issues"]


def test_controller_audit_detects_the_started_prompt_hash_drifting(tmp_path: Path):
    """CON-5, CON-7: audit verifies the exact prompt actually used for the session."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    session = json.loads((session_dir / "session.json").read_text())
    session["run_prompt_sha256"] = "0" * 64
    (session_dir / "session.json").write_text(json.dumps(session))

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )

    assert returncode == 1
    assert response["ok"] is False
    audit = json.loads((session_dir / "audit.json").read_text())
    assert "PROMPT_DRIFT" in audit["issues"]


def test_controller_audit_detects_heldout_exposure_and_local_rollout_bypass(
    tmp_path: Path,
):
    """CON-7: agent-time held-out seeds and local-baseline simulator runs are exclusions."""
    from tools import s1_harness_ablation as controller

    campaign, result, runtime = _prepare_controller_campaign(controller, tmp_path)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    leaked_run = session_dir / "worktree" / "runs" / "agent-leak"
    leaked_run.mkdir(parents=True)
    (leaked_run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "agent-leak",
                "env_baseline": "local",
                "env_baseline_oid": None,
            }
        )
    )
    (leaked_run / "episodes.jsonl").write_text(
        json.dumps({"episode": 0, "seed": 100, "status": "fail"}) + "\n"
    )

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )

    assert returncode == 1
    assert response["ok"] is False
    audit = json.loads((session_dir / "audit.json").read_text())
    assert {"HELDOUT_EXPOSURE", "UNTRUSTED_ROLLOUT"}.issubset(audit["issues"])
    assert {"HELDOUT_EXPOSURE", "UNTRUSTED_ROLLOUT"}.issubset(audit["exclusions"])


def test_controller_cli_emits_one_json_object_even_for_argument_errors(tmp_path: Path):
    """CON-8: controller diagnostics never mix usage text into stdout."""
    from tools import s1_harness_ablation as controller

    runtime = _controller_runtime(controller)
    returncode, response, output, stderr = _invoke_controller(
        controller, ["prepare", "--pairs", "1"], runtime
    )

    assert returncode == 1
    assert response["ok"] is False
    assert response["code"] == "ARGUMENT"
    assert output.count("\n") == 1
    assert "usage:" not in output
    assert "usage:" not in stderr


def test_controller_supervises_dev_attempts_through_assigned_adapter_and_correlates_audit(
    tmp_path: Path,
):
    """CON-5, CON-7: controller admits, records, and settles every assigned dev attempt."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    adapter_calls: list[tuple[str, Path, str, str]] = []

    class DevelopmentAdapter:
        def __init__(self, condition: str):
            self.condition = condition

        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            adapter_calls.append((self.condition, candidate, seeds, run_id))
            episodes = tuple(
                _episode_record(index, seed)
                for index, seed in enumerate(int(value) for value in seeds.split(","))
            )
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.1),
                episodes=episodes,
                failures={"timeout": len(episodes)},
                safety=SafetyResult(ungated=0, clamps=1, extra_item=0),
                timing={"wall_s": 1.0, "sim_s": 2.0},
                artifacts={},
            )

    base_runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: DevelopmentAdapter(condition),
    )
    nested_responses: list[dict] = []
    runtime = None

    def execute(command, cwd, agent, on_line, stop_reason, wall_ceiling_s, environment):
        assert environment["AISLE_ABLATION_SESSION"]
        assert environment["AISLE_ABLATION_CONTROLLER"].endswith("tools/s1_harness_ablation.py")
        response = controller._attempt_client(
            controller.argparse.Namespace(
                session=environment["AISLE_ABLATION_SESSION"],
                seeds="0,1",
            ),
            environment,
        )
        assert response["ok"] is True
        nested_responses.append(response)
        line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 3,
                            "cache_creation_input_tokens": 2,
                            "output_tokens": 1,
                        }
                    },
                }
            )
            + "\n"
        )
        on_line(line, 0.25)
        return controller.ExecutionResult(
            stopped="agent_done",
            returncode=0,
            wall_s=0.25,
            agent_version="fixture-agent-1",
        )

    runtime = replace(base_runtime, execute_agent=execute)
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]

    returncode, response, _, _ = _run_prepared_controller_session(controller, session_dir, runtime)

    assert returncode == 0 and response["ok"] is True
    assert nested_responses[0]["ok"] is True
    attempt = json.loads((session_dir / "attempts.jsonl").read_text())
    assert attempt["attempt_id"] == nested_responses[0]["attempt_id"]
    assert [episode["seed"] for episode in attempt["episodes"]] == [0, 1]
    session = json.loads((session_dir / "session.json").read_text())
    assert adapter_calls == [
        (
            session["condition"],
            session_dir / session["candidate"],
            "0,1",
            nested_responses[0]["attempt_id"],
        )
    ]
    controller_manifest = (
        session_dir
        / "worktree"
        / "runs"
        / nested_responses[0]["attempt_id"]
        / "controller_attempt.json"
    )
    assert controller_manifest.is_file()

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )
    assert returncode == 0
    assert response["ok"] is True
    assert response["sessions"][0]["development_episodes"] == 2

    controller_manifest.unlink()
    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )
    assert returncode == 1
    assert "ATTEMPT_CORRELATION_INVALID" in response["sessions"][0]["issues"]


def test_controller_attempt_channel_refuses_heldout_and_exhausted_dev_budget(tmp_path: Path):
    """CON-7: attempt admission rejects non-dev seeds and reserves capacity before launch."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    launches: list[str] = []

    class Adapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            launches.append(seeds)
            seed_values = [int(value) for value in seeds.split(",")]
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
                episodes=tuple(
                    _episode_record(index, seed) for index, seed in enumerate(seed_values)
                ),
                failures={"timeout": len(seed_values)},
                safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
                timing={"wall_s": 0.0, "sim_s": 0.0},
                artifacts={},
            )

    runtime = _controller_runtime(controller, adapter_factory=lambda condition, worktree: Adapter())
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    session = json.loads((session_dir / "session.json").read_text())
    session.update(state="running", budgets={"tokens": 500000, "episodes": 2, "wall_h": 4})
    (session_dir / "session.json").write_text(json.dumps(session))

    heldout_rc, heldout, _, _ = _invoke_controller(
        controller,
        [
            "attempt",
            "--session",
            str(session_dir),
            "--seeds",
            "100",
        ],
        runtime,
    )
    first_rc, first, _, _ = _invoke_controller(
        controller,
        [
            "attempt",
            "--session",
            str(session_dir),
            "--seeds",
            "0,1",
        ],
        runtime,
    )
    exhausted_rc, exhausted, _, _ = _invoke_controller(
        controller,
        [
            "attempt",
            "--session",
            str(session_dir),
            "--seeds",
            "2",
        ],
        runtime,
    )

    assert heldout_rc == 1 and heldout["code"] == "SEED_DOMAIN"
    assert first_rc == 0 and first["ok"] is True
    assert exhausted_rc == 1 and exhausted["code"] == "EPISODE_BUDGET"
    assert launches == ["0,1"]


def test_controller_audit_rejects_unowned_launch_and_attempt_correlation_tamper(
    tmp_path: Path,
):
    """CON-7: every development launch has exactly one controller admission and manifest."""
    from tools import s1_harness_ablation as controller

    runtime = _controller_runtime(controller)
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    orphan = session_dir / "worktree" / "runs" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "orphan",
                "env_baseline": "origin/main",
                "env_baseline_oid": "a" * 40,
            }
        )
    )
    (orphan / "episodes.jsonl").write_text("")

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )

    assert returncode == 1
    assert response["ok"] is False
    audit = json.loads((session_dir / "audit.json").read_text())
    assert "UNOWNED_LAUNCH" in audit["issues"]


def test_script_rollout_explicit_root_pins_wrapper_runs_and_pythonpath(tmp_path: Path, monkeypatch):
    """CON-5, CON-7: script execution resolves every trusted path inside its session worktree."""
    from aisle.harness import script_rollout

    graph_dir = tmp_path / "graphs"
    graph_dir.mkdir()
    (graph_dir / "ablation_script_s1_wrapper.yaml").write_text(
        (REPO_ROOT / "graphs" / "ablation_script_s1_wrapper.yaml").read_text()
    )
    policy = tmp_path / "baselines" / "script_s1" / "candidate.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("def create_policy(seed):\n    return object()\n")
    captured: dict[str, object] = {}

    class CompletedGraph:
        pid = 999_999_999

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def launch(graph: Path, run_dir: Path, env: dict, stderr):
        captured.update(graph=graph, run_dir=run_dir, env=env)
        return CompletedGraph()

    monkeypatch.setattr(script_rollout, "_spawn_script_dora", launch)
    monkeypatch.setattr(script_rollout, "_terminate_script", lambda proc: None)
    monkeypatch.setattr(script_rollout, "reap_orphans", lambda run_dir: None)

    script_rollout.run_script_rollout(policy, "3", "pinned-script", root=tmp_path)

    assert Path(captured["run_dir"]) == tmp_path / "runs" / "pinned-script"
    assert Path(captured["graph"]).is_relative_to(tmp_path / "runs" / "pinned-script")
    assert captured["env"]["PYTHONPATH"] == f"{tmp_path / 'src'}:{tmp_path}"
    assert captured["env"]["AISLE_SCRIPT_POLICY"] == str(policy)


def test_script_preflight_explicit_root_executes_the_pinned_worker(tmp_path: Path):
    """CON-5, CON-7: script preflight never imports a worker from the controller checkout."""
    from aisle.harness.script_preflight import _worker_command

    candidate = tmp_path / "candidate.py"

    assert _worker_command(candidate, root=tmp_path) == [
        sys.executable,
        "-I",
        str(tmp_path / "src" / "aisle" / "harness" / "script_preflight_worker.py"),
        str(candidate.resolve()),
    ]


def test_controller_script_factory_injects_the_session_worktree_root(tmp_path: Path, monkeypatch):
    """CON-5: the controller pins both script gates to the assigned worktree."""
    from aisle.harness import ablation_adapters
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    roots: list[Path] = []

    def preflight(candidate: Path, *, root: Path):
        roots.append(root)
        return PreflightResult(ok=True, errors=(), wall_s=0.0)

    def rollout(candidate: Path, seeds: str, run_id: str, *, root: Path):
        roots.append(root)
        return AttemptResult(
            attempt_id=run_id,
            candidate_hash=sha256_file(candidate),
            preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
            episodes=(),
            failures={"ROLLOUT_INCOMPLETE": 1},
            safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
            timing={"wall_s": 0.0, "sim_s": 0.0},
            artifacts={},
        )

    monkeypatch.setattr(ablation_adapters, "preflight_script", preflight)
    monkeypatch.setattr(ablation_adapters, "run_script_rollout", rollout)
    candidate = tmp_path / "candidate.py"
    candidate.write_text("def create_policy(seed):\n    return object()\n")

    adapter = controller.default_adapter_factory("script", tmp_path)
    adapter.rollout(candidate, "3", "factory-pinned")

    assert roots == [tmp_path.resolve(), tmp_path.resolve()]


def test_controller_scoring_admission_is_terminal_after_adapter_crash(tmp_path: Path, monkeypatch):
    """CON-5, CON-7: held-out admission is persisted once before a crashing adapter."""
    from tools import s1_harness_ablation as controller

    calls = 0

    class CrashingAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str):
            nonlocal calls
            calls += 1
            raise RuntimeError("fixture scorer crash")

    monkeypatch.setattr(controller, "_new_nonce", lambda: "score-once", raising=False)
    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: CrashingAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)

    first_rc, first, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )
    second_rc, second, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert first_rc == 1
    assert first["ok"] is False
    assert second_rc == 1 and second["code"] == "SCORING_TERMINAL"
    assert calls == 1
    session = json.loads((session_dir / "session.json").read_text())
    holdout = json.loads((session_dir / "holdout.json").read_text())
    assert session["state"] == "scoring_started"
    assert holdout["state"] == "scoring_started"
    assert session["scoring_admission"]["nonce"] == "score-once"


def test_controller_scoring_result_must_match_its_nonce_admission(tmp_path: Path):
    """CON-7: a held-out adapter cannot substitute a result from another run."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    class SubstitutingAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            return AttemptResult(
                attempt_id="different-run",
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
                episodes=tuple(
                    _episode_record(index, seed) for index, seed in enumerate(range(100, 108))
                ),
                failures={"timeout": 8},
                safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
                timing={"wall_s": 0.0, "sim_s": 0.0},
                artifacts={},
            )

    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: SubstitutingAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "INFRA_PROTOCOL"
    assert json.loads((session_dir / "session.json").read_text())["state"] == ("scoring_started")


def test_controller_serializes_concurrent_holdout_admission(tmp_path: Path):
    """CON-7: concurrent scorers cannot both cross the single-use admission boundary."""
    from aisle.harness.ablation import (
        AttemptResult,
        PreflightResult,
        SafetyResult,
        sha256_file,
    )
    from tools import s1_harness_ablation as controller

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            calls.append(run_id)
            entered.set()
            assert release.wait(timeout=2)
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash=sha256_file(candidate),
                preflight=PreflightResult(ok=True, errors=(), wall_s=0.0),
                episodes=tuple(
                    _episode_record(index, seed) for index, seed in enumerate(range(100, 108))
                ),
                failures={"timeout": 8},
                safety=SafetyResult(ungated=0, clamps=0, extra_item=0),
                timing={"wall_s": 0.0, "sim_s": 0.0},
                artifacts={},
            )

    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: BlockingAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    args = controller.argparse.Namespace(session=str(session_dir), holdout="100..107")
    outcomes: list[object] = []

    def score() -> None:
        try:
            outcomes.append(controller._score(args, runtime))
        except controller.ControllerError as exc:
            outcomes.append(exc)

    first = threading.Thread(target=score)
    second = threading.Thread(target=score)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    assert len(calls) == 1
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(calls) == 1
    assert sum(isinstance(outcome, controller.ControllerError) for outcome in outcomes) == 1
    assert json.loads((session_dir / "session.json").read_text())["state"] == "scored"


def test_controller_attempt_broker_authenticates_requests(tmp_path: Path):
    """CON-7: an IPC caller without the controller capability cannot launch an adapter."""
    from tools import s1_harness_ablation as controller

    runtime = _controller_runtime(controller)
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    args = controller.argparse.Namespace(session=str(session_dir), seeds="0")

    with controller.AttemptBroker(session_dir, runtime) as broker:
        environment = broker.environment()
        environment["AISLE_ABLATION_CAPABILITY"] = "wrong"
        response = controller._attempt_client(args, environment)

    assert response["ok"] is False
    assert response["code"] == "ATTEMPT_BROKER_AUTH"


def test_controller_audit_charges_crashed_attempt_admission_conservatively(tmp_path: Path):
    """CON-7: an unsettled adapter launch consumes its full requested reservation."""
    from tools import s1_harness_ablation as controller

    class CrashingAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str):
            raise RuntimeError("fixture development crash")

    runtime = _controller_runtime(
        controller,
        adapter_factory=lambda condition, worktree: CrashingAdapter(),
    )
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    session = json.loads((session_dir / "session.json").read_text())
    session.update(state="running", budgets={"tokens": 500000, "episodes": 2, "wall_h": 4})
    (session_dir / "session.json").write_text(json.dumps(session))

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["attempt", "--session", str(session_dir), "--seeds", "0,1"],
        runtime,
    )
    assert returncode == 1 and response["code"] == "INTERNAL"

    _, audit, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )
    session_audit = audit["sessions"][0]
    assert session_audit["development_episodes"] == 2
    assert "ATTEMPT_CORRELATION_INVALID" in session_audit["issues"]


def test_controller_holdout_admission_refuses_preexisting_nonce_run(tmp_path: Path, monkeypatch):
    """CON-7: a pre-admission same-ID manifest can never gain scorer exemption."""
    from tools import s1_harness_ablation as controller

    monkeypatch.setattr(controller, "_new_nonce", lambda: "collision", raising=False)
    runtime = _controller_runtime(controller)
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]
    _run_prepared_controller_session(controller, session_dir, runtime)
    collision = session_dir / "worktree" / "runs" / f"holdout-{result['sessions'][0]}-collision"
    collision.mkdir(parents=True)
    (collision / "manifest.json").write_text(
        json.dumps({"run_id": collision.name, "env_baseline": "origin/main"})
    )

    returncode, response, _, _ = _invoke_controller(
        controller,
        ["score", "--session", str(session_dir), "--holdout", "100..107"],
        runtime,
    )

    assert returncode == 1
    assert response["code"] == "HOLDOUT_RUN_COLLISION"
    assert json.loads((session_dir / "session.json").read_text())["state"] == ("scoring_started")


@pytest.mark.parametrize(
    ("agent", "event"),
    [
        ("claude", {"type": "assistant", "message": {"usage": {}}}),
        (
            "claude",
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 1}},
            },
        ),
        (
            "codex",
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ],
)
def test_controller_partial_vendor_usage_objects_fail_closed(agent: str, event: dict):
    """HAR-5: every mandated vendor usage field must be present and exact."""
    from tools import s1_harness_ablation as controller

    with pytest.raises(controller.TelemetryError):
        controller._agent_event(json.dumps(event), agent)


def test_controller_token_overshoot_and_wall_stop_are_nonzero_terminal_outcomes(
    tmp_path: Path,
):
    """HAR-5, CON-8: external token or wall enforcement never reports a successful run."""
    from tools import s1_harness_ablation as controller

    overshoot = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 500_000,
                        "cache_creation_input_tokens": 1,
                        "output_tokens": 0,
                    }
                },
            }
        )
        + "\n"
    )
    token_runtime = _controller_runtime(controller, agent_lines=(overshoot,))
    token_campaign, token_result, _ = _prepare_controller_campaign(
        controller, tmp_path / "token", runtime=token_runtime
    )
    token_session = token_campaign / token_result["sessions"][0]

    token_rc, token_response, _, _ = _run_prepared_controller_session(
        controller, token_session, token_runtime
    )

    assert token_rc == 1
    assert token_response["code"] == "TOKEN_BUDGET_EXCEEDED"
    assert json.loads((token_session / "session.json").read_text())["state"] == ("budget_exceeded")

    base_runtime = _controller_runtime(controller)

    def wall_executor(command, cwd, agent, on_line, stop_reason, wall_ceiling_s, environment):
        line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 1,
                        }
                    },
                }
            )
            + "\n"
        )
        on_line(line, wall_ceiling_s)
        return controller.ExecutionResult(
            stopped="wall_budget",
            returncode=-signal.SIGKILL,
            wall_s=wall_ceiling_s,
            agent_version="fixture-agent-1",
        )

    wall_runtime = replace(base_runtime, execute_agent=wall_executor)
    wall_campaign, wall_result, _ = _prepare_controller_campaign(
        controller, tmp_path / "wall", runtime=wall_runtime
    )
    wall_session = wall_campaign / wall_result["sessions"][0]
    wall_rc, wall_response, _, _ = _run_prepared_controller_session(
        controller, wall_session, wall_runtime
    )

    assert wall_rc == 1
    assert wall_response["code"] == "WALL_BUDGET_EXCEEDED"
    assert json.loads((wall_session / "session.json").read_text())["state"] == ("budget_exceeded")


def test_controller_unbrokered_simulator_observation_is_terminal_and_audited(
    tmp_path: Path,
):
    """CON-7: an observed agent-owned simulator child terminates and excludes the session."""
    from tools import s1_harness_ablation as controller

    base_runtime = _controller_runtime(controller)

    def executor(command, cwd, agent, on_line, stop_reason, wall_ceiling_s, environment):
        line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 1,
                        }
                    },
                }
            )
            + "\n"
        )
        on_line(line, 0.1)
        return controller.ExecutionResult(
            stopped="unowned_launch",
            returncode=-signal.SIGKILL,
            wall_s=0.1,
            agent_version="fixture-agent-1",
        )

    runtime = replace(base_runtime, execute_agent=executor)
    campaign, result, _ = _prepare_controller_campaign(controller, tmp_path, runtime=runtime)
    session_dir = campaign / result["sessions"][0]

    returncode, response, _, _ = _run_prepared_controller_session(controller, session_dir, runtime)
    assert returncode == 1 and response["code"] == "UNOWNED_LAUNCH"

    _, audit, _, _ = _invoke_controller(
        controller,
        ["audit", "--dir", str(session_dir)],
        runtime,
    )
    assert "UNOWNED_LAUNCH" in audit["sessions"][0]["exclusions"]


def test_controller_detects_agent_descendant_harness_rollout_from_fake_proc(
    tmp_path: Path,
):
    """CON-7: process evidence distinguishes an agent-owned launch from broker children."""
    from tools import s1_harness_ablation as controller

    for pid, parent, command in (
        (100, 1, b"claude\0"),
        (101, 100, b"/fixture/bin/harness\0rollout\0--seeds\00\0"),
        (102, 1, b"dora\0run\0broker-owned\0"),
    ):
        process = tmp_path / str(pid)
        process.mkdir()
        (process / "status").write_text(f"Name:\tfixture\nPPid:\t{parent}\n")
        (process / "cmdline").write_bytes(command)

    assert controller._has_unowned_simulator_descendant(100, tmp_path) is True
    assert controller._has_unowned_simulator_descendant(102, tmp_path) is False


def test_controller_stdout_eof_wait_is_bounded_by_remaining_wall(tmp_path: Path, monkeypatch):
    """CON-7: an agent closing stdout cannot make the controller wait past wall budget."""
    from tools import s1_harness_ablation as controller

    killed: list[int] = []

    class EmptyStdout:
        def __iter__(self):
            return iter(())

        def close(self):
            return None

    class StuckAfterEof:
        pid = 12345
        stdout = EmptyStdout()

        def poll(self):
            return None

        def wait(self, timeout=None):
            if killed:
                return -signal.SIGKILL
            if timeout is None:
                raise AssertionError("unbounded wait after stdout EOF")
            raise subprocess.TimeoutExpired(["fixture-agent"], timeout)

    monkeypatch.setattr(controller.subprocess, "Popen", lambda *args, **kwargs: StuckAfterEof())
    monkeypatch.setattr(controller.os, "killpg", lambda pid, sig: killed.append(sig))
    monkeypatch.setattr(controller, "_agent_version", lambda agent: "fixture-agent-1")

    result = controller._execute_agent(
        ["fixture-agent"],
        tmp_path,
        "claude",
        lambda line, wall: None,
        lambda wall: None,
        0.01,
        {},
    )

    assert result.stopped == "wall_budget"
    assert killed == [signal.SIGKILL]


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"]])
def test_controller_help_is_one_success_json_object(argv: list[str]):
    """CON-8: help follows the same single-JSON and exit-status contract."""
    from tools import s1_harness_ablation as controller

    stdout = StringIO()
    with redirect_stdout(stdout):
        returncode = controller.main(argv)
    response = json.loads(stdout.getvalue())

    assert returncode == 0
    assert response["ok"] is True
    assert isinstance(response["usage"], dict)
    assert stdout.getvalue().count("\n") == 1


@pytest.fixture
def native_sandbox_worktrees_root(tmp_path: Path, monkeypatch) -> Path:
    from aisle.harness import native_sandbox

    repository = tmp_path / "sandbox-policy-repository"
    worktrees_root = repository / ".worktrees"
    controller = worktrees_root / "trusted-controller"
    host_venv = repository / ".venv"
    for directory in (worktrees_root, controller, host_venv):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(native_sandbox, "_CONTROLLER_ROOT", controller)
    monkeypatch.setattr(native_sandbox, "_WORKTREES_ROOT", worktrees_root)
    monkeypatch.setattr(native_sandbox, "_HOST_SIMULATION_ENV", host_venv)
    monkeypatch.setattr(
        native_sandbox,
        "_PROTECTED_SOURCES",
        (controller, worktrees_root, host_venv),
    )
    return worktrees_root


def _native_sandbox_policy(tmp_path: Path, worktrees_root: Path, *, agent_name: str = "claude"):
    from aisle.harness.native_sandbox import SandboxPolicy

    worktree = worktrees_root / "assigned-session-worktree"
    agent_env = tmp_path / "minimal-agent-environment"
    runtime_dir = tmp_path / "private-attempt-runtime"
    client = tmp_path / "immutable-attempt-client"
    executable = tmp_path / f"selected-{agent_name}"
    credentials = tmp_path / f".{agent_name}-credentials"
    for directory in (worktree, agent_env, runtime_dir, credentials):
        directory.mkdir()
    runtime_dir.chmod(0o700)
    client.write_text("#!/agent-env/bin/python\n")
    executable.write_text("#!/bin/sh\n")
    return SandboxPolicy(
        worktree=worktree,
        agent_env=agent_env,
        runtime_dir=runtime_dir,
        attempt_client=client,
        agent_executable=executable,
        credential_mounts=(credentials,),
    )


def _bwrap_mounts(argv: list[str], option: str) -> list[tuple[str, str]]:
    return [
        (argv[index + 1], argv[index + 2]) for index, value in enumerate(argv) if value == option
    ]


def test_native_sandbox_builds_an_explicit_minimal_namespace(
    tmp_path: Path, native_sandbox_worktrees_root: Path
):
    """CON-5: the native isolation policy produces deterministic, explicit bwrap argv."""
    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root)
    argv = build_bwrap_argv(
        policy,
        ["/opt/aisle/agent", "--version"],
        {
            "ANTHROPIC_API_KEY": "fixture-anthropic-key",
            "HTTPS_PROXY": "http://fixture-proxy.invalid",
            "LANG": "C.UTF-8",
            "SSL_CERT_FILE": "/fixture/cert.pem",
            "UNRELATED_HOST_SECRET": "must-not-cross",
        },
    )

    assert Path(argv[0]).name == "bwrap"
    for flag in (
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
    ):
        assert argv.count(flag) == 1
    assert ("--proc", "/proc") in zip(argv, argv[1:], strict=False)
    assert ("--dev", "/dev") in zip(argv, argv[1:], strict=False)
    assert argv[-3:] == ["--", "/opt/aisle/agent", "--version"]

    writable = _bwrap_mounts(argv, "--bind")
    readonly = _bwrap_mounts(argv, "--ro-bind")
    assert writable == [(str(policy.worktree.resolve()), "/workspace")]
    assert (str(policy.agent_env.resolve()), "/agent-env") in readonly
    assert (str(policy.runtime_dir.resolve()), "/run/aisle") in readonly
    assert (str(policy.attempt_client.resolve()), "/opt/aisle/attempt-client") in readonly
    assert (str(policy.agent_executable.resolve()), "/opt/aisle/agent") in readonly
    assert (
        str(policy.credential_mounts[0].resolve()),
        f"/agent-home/{policy.credential_mounts[0].name}",
    ) in readonly
    assert any(destination in {"/lib", "/usr/lib"} for _, destination in readonly)
    assert any(destination.startswith("/etc/") for _, destination in readonly)
    mounted_sources = {Path(source) for source, _ in writable + readonly}
    assert not mounted_sources & {
        Path("/dev/nvidia0"),
        Path("/dev/nvidiactl"),
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/run/containerd/containerd.sock"),
        REPO_ROOT,
        REPO_ROOT.parent,
        REPO_ROOT.parent.parent / ".venv",
    }
    assert ("--tmpfs", "/tmp") in zip(argv, argv[1:], strict=False)
    assert ("--tmpfs", "/agent-home") in zip(argv, argv[1:], strict=False)

    setenv = {
        argv[index + 1]: argv[index + 2] for index, value in enumerate(argv) if value == "--setenv"
    }
    assert setenv == {
        "AISLE_ABLATION_CLIENT": "/opt/aisle/attempt-client",
        "AISLE_ABLATION_SOCKET": "/run/aisle/attempt.sock",
        "ANTHROPIC_API_KEY": "fixture-anthropic-key",
        "HOME": "/agent-home",
        "HTTPS_PROXY": "http://fixture-proxy.invalid",
        "LANG": "C.UTF-8",
        "PATH": "/agent-env/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "SSL_CERT_FILE": "/fixture/cert.pem",
    }
    assert "UNRELATED_HOST_SECRET" not in argv


def test_native_sandbox_policy_is_deeply_immutable(
    tmp_path: Path, native_sandbox_worktrees_root: Path
):
    """CON-5: sandbox mount authority cannot drift after policy construction."""
    from dataclasses import FrozenInstanceError

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root)

    with pytest.raises(FrozenInstanceError):
        policy.worktree = tmp_path / "replacement"
    assert isinstance(policy.credential_mounts, tuple)


@pytest.mark.parametrize(
    "forbidden",
    [
        Path("/dev/nvidia0"),
        Path("/dev/nvidiactl"),
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/run/containerd/containerd.sock"),
    ],
)
def test_native_sandbox_rejects_forbidden_bind_sources(
    tmp_path: Path, native_sandbox_worktrees_root: Path, forbidden: Path
):
    """Native isolation design: GPU and container-control paths fail closed."""
    from dataclasses import replace

    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = replace(
        _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root),
        credential_mounts=(forbidden,),
    )

    with pytest.raises(ValueError, match="forbidden"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


@pytest.mark.parametrize(
    "forbidden_ancestor",
    [
        Path("/dev"),
        Path("/run"),
        Path("/var/run"),
        Path("/proc"),
        Path("/sys"),
    ],
)
def test_native_sandbox_rejects_pseudofs_and_forbidden_ancestors(
    tmp_path: Path, native_sandbox_worktrees_root: Path, forbidden_ancestor: Path
):
    """Native isolation design: alternate mounts cannot restore host devices or processes."""
    from dataclasses import replace

    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = replace(
        _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root),
        credential_mounts=(forbidden_ancestor,),
    )

    with pytest.raises(ValueError, match="forbidden"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


@pytest.mark.parametrize(
    "protected_source_name",
    ["controller", "worktrees", "host_venv"],
)
def test_native_sandbox_rejects_controller_worktree_parent_and_host_venv(
    tmp_path: Path, native_sandbox_worktrees_root: Path, protected_source_name: str
):
    """CON-7: trusted controller, sibling-worktree parent, and simulation env stay hidden."""
    from dataclasses import replace

    from aisle.harness import native_sandbox

    protected_sources = {
        "controller": native_sandbox._CONTROLLER_ROOT,
        "worktrees": native_sandbox_worktrees_root,
        "host_venv": native_sandbox._HOST_SIMULATION_ENV,
    }

    policy = replace(
        _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root),
        credential_mounts=(protected_sources[protected_source_name],),
    )

    with pytest.raises(ValueError, match="protected"):
        native_sandbox.build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


@pytest.mark.parametrize("worktree_kind", ["outside", "nested", "root", "controller"])
def test_native_sandbox_rejects_every_non_direct_child_worktree(
    tmp_path: Path, native_sandbox_worktrees_root: Path, worktree_kind: str
):
    """Native isolation design: /workspace is exactly one configured worktree leaf."""
    from dataclasses import replace

    from aisle.harness import native_sandbox

    candidates = {
        "outside": tmp_path / "outside-worktree",
        "nested": native_sandbox_worktrees_root / "session" / "nested",
        "root": native_sandbox_worktrees_root,
        "controller": native_sandbox._CONTROLLER_ROOT,
    }
    candidate = candidates[worktree_kind]
    candidate.mkdir(parents=True, exist_ok=True)
    policy = replace(
        _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root),
        worktree=candidate,
    )

    with pytest.raises(ValueError, match="worktree"):
        native_sandbox.build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


def test_native_sandbox_allows_one_assigned_leaf_under_worktrees_root(tmp_path: Path, monkeypatch):
    """Native isolation design: the assigned leaf is writable while its parent stays hidden."""
    from dataclasses import replace

    from aisle.harness import native_sandbox

    repository = tmp_path / "trusted-repository"
    worktrees_root = repository / ".worktrees"
    assigned = worktrees_root / "assigned-session"
    assigned.mkdir(parents=True)
    host_venv = repository / ".venv"
    host_venv.mkdir()
    monkeypatch.setattr(native_sandbox, "_CONTROLLER_ROOT", repository)
    monkeypatch.setattr(native_sandbox, "_WORKTREES_ROOT", worktrees_root)
    monkeypatch.setattr(native_sandbox, "_HOST_SIMULATION_ENV", host_venv)
    monkeypatch.setattr(
        native_sandbox,
        "_PROTECTED_SOURCES",
        (repository, worktrees_root, host_venv),
    )
    policy = replace(_native_sandbox_policy(tmp_path, worktrees_root), worktree=assigned)

    argv = native_sandbox.build_bwrap_argv(policy, ["/opt/aisle/agent"], {})

    assert (str(assigned.resolve()), "/workspace") in _bwrap_mounts(argv, "--bind")
    assert all(
        Path(source) not in {repository.resolve(), worktrees_root.resolve()}
        for source, _ in _bwrap_mounts(argv, "--ro-bind")
    )


@pytest.mark.parametrize("relative_worktree", [Path("src"), Path(".worktrees/session/nested")])
def test_native_sandbox_rejects_controller_and_nested_worktree_paths(
    tmp_path: Path, monkeypatch, relative_worktree: Path
):
    """CON-7: the writable exception cannot select controller source or a nested subtree."""
    from dataclasses import replace

    from aisle.harness import native_sandbox

    repository = tmp_path / "trusted-repository"
    worktrees_root = repository / ".worktrees"
    host_venv = repository / ".venv"
    candidate = repository / relative_worktree
    for directory in (worktrees_root, host_venv, candidate):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(native_sandbox, "_CONTROLLER_ROOT", repository)
    monkeypatch.setattr(native_sandbox, "_WORKTREES_ROOT", worktrees_root)
    monkeypatch.setattr(native_sandbox, "_HOST_SIMULATION_ENV", host_venv)
    monkeypatch.setattr(
        native_sandbox,
        "_PROTECTED_SOURCES",
        (repository, worktrees_root, host_venv),
    )
    policy = replace(_native_sandbox_policy(tmp_path, worktrees_root), worktree=candidate)

    with pytest.raises(ValueError, match="worktree"):
        native_sandbox.build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


def test_native_sandbox_requires_private_uid_owned_runtime(
    tmp_path: Path, native_sandbox_worktrees_root: Path, monkeypatch
):
    """Native isolation design: socket runtime ownership and mode are fail-closed."""
    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root)
    policy.runtime_dir.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})

    policy.runtime_dir.chmod(0o700)
    monkeypatch.setattr("aisle.harness.native_sandbox.os.getuid", lambda: -1)
    with pytest.raises(ValueError, match="current uid"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


@pytest.mark.parametrize("source_field", ["runtime_dir", "attempt_client"])
def test_native_sandbox_keeps_socket_and_client_outside_writable_worktree(
    tmp_path: Path, native_sandbox_worktrees_root: Path, source_field: str
):
    """Native isolation design: broker authority cannot live in agent-writable storage."""
    from dataclasses import replace

    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root)
    if source_field == "runtime_dir":
        source = policy.worktree / "runtime"
        source.mkdir()
        source.chmod(0o700)
    else:
        source = policy.worktree / "attempt-client"
        source.write_text("immutable only outside the worktree\n")
    policy = replace(policy, **{source_field: source})

    with pytest.raises(ValueError, match="outside|protected"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


@pytest.mark.parametrize("source_field", ["runtime_dir", "credential_mounts"])
def test_native_sandbox_rejects_bind_ancestors_of_writable_worktree(
    tmp_path: Path, native_sandbox_worktrees_root: Path, source_field: str
):
    """Native isolation design: alternate mounts cannot reveal the worktree through an ancestor."""
    from dataclasses import replace

    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root)
    tmp_path.chmod(0o700)
    replacement = tmp_path if source_field == "runtime_dir" else (tmp_path,)
    policy = replace(policy, **{source_field: replacement})

    with pytest.raises(ValueError, match="outside|protected"):
        build_bwrap_argv(policy, ["/opt/aisle/agent"], {})


def test_native_sandbox_selects_only_matching_vendor_auth(
    tmp_path: Path, native_sandbox_worktrees_root: Path
):
    """Native isolation design: vendor selection cannot leak another provider's credentials."""
    from aisle.harness.native_sandbox import build_bwrap_argv

    policy = _native_sandbox_policy(tmp_path, native_sandbox_worktrees_root, agent_name="codex")
    argv = build_bwrap_argv(
        policy,
        ["/opt/aisle/agent"],
        {
            "OPENAI_API_KEY": "fixture-openai-key",
            "ANTHROPIC_API_KEY": "must-not-cross",
            "HTTP_PROXY": "http://fixture-proxy.invalid",
        },
    )

    assert "OPENAI_API_KEY" in argv
    assert "fixture-openai-key" in argv
    assert "ANTHROPIC_API_KEY" not in argv
    assert "must-not-cross" not in argv


def _valid_native_sandbox_probe() -> dict[str, bool]:
    return {
        "worktree_write": True,
        "network_dns": True,
        "host_process_visible": False,
        "nvidia_visible": False,
        "docker_socket_visible": False,
        "genesis_importable": False,
        "dora_executable": False,
        "other_worktree_visible": False,
        "attempt_socket_visible": True,
    }


def test_native_sandbox_probe_accepts_only_the_exact_success_contract():
    """Native isolation design: every harmless isolation probe must have its safe value."""
    from aisle.harness.native_sandbox import verify_sandbox_probe

    assert verify_sandbox_probe(_valid_native_sandbox_probe()) == (True, ())


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        (lambda result: result.pop("network_dns"), "missing:network_dns"),
        (lambda result: result.update({"surprise": True}), "unknown:surprise"),
        (lambda result: result.update({1: True, "surprise": True}), "unknown:1"),
        (lambda result: result.update({"nvidia_visible": True}), "nvidia_visible"),
        (lambda result: result.update({"attempt_socket_visible": 1}), "attempt_socket_visible"),
    ],
)
def test_native_sandbox_probe_fails_closed_on_shape_value_or_type(mutation, detail: str):
    """Native isolation design: missing, unknown, unsafe, and truthy non-bools are rejected."""
    from aisle.harness.native_sandbox import verify_sandbox_probe

    result = _valid_native_sandbox_probe()
    mutation(result)

    ok, errors = verify_sandbox_probe(result)

    assert ok is False
    assert any(detail in error for error in errors)


def test_native_sandbox_probe_rejects_a_non_object_result():
    """Native isolation design: malformed probe output fails closed instead of raising."""
    from aisle.harness.native_sandbox import verify_sandbox_probe

    assert verify_sandbox_probe([]) == (False, ("result:not_object",))


def test_native_sandbox_probe_command_is_json_only_and_harmless():
    """CON-8: the probe command uses filesystem/import/path checks and DNS only."""
    from aisle.harness.native_sandbox import sandbox_probe_command

    command = sandbox_probe_command()

    assert command[:3] == ["/agent-env/bin/python", "-I", "-c"]
    assert len(command) == 4
    compile(command[3], "<sandbox-probe>", "exec")
    assert "getaddrinfo" in command[3]
    assert "find_spec" in command[3]
    assert "subprocess" not in command[3]
    assert "urlopen" not in command[3]


def _execute_native_sandbox_probe(source: str) -> dict:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(compile(source, "<sandbox-probe>", "exec"), {})
    return json.loads(stdout.getvalue())


def test_native_sandbox_probe_distinguishes_host_pid_namespace(monkeypatch):
    """Native isolation design: host visibility means matching trusted PID-namespace identity."""
    from aisle.harness.native_sandbox import sandbox_probe_command

    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    host_namespace = os.stat("/proc/self/ns/pid").st_ino
    source = sandbox_probe_command()[3]

    assert _execute_native_sandbox_probe(source)["host_process_visible"] is True
    isolated_source = source.replace(
        f"HOST_PID_NAMESPACE = {host_namespace}",
        f"HOST_PID_NAMESPACE = {host_namespace + 1}",
    )
    assert isolated_source != source
    assert _execute_native_sandbox_probe(isolated_source)["host_process_visible"] is False


@pytest.mark.parametrize(
    "probe_key",
    [
        "host_process_visible",
        "nvidia_visible",
        "docker_socket_visible",
        "genesis_importable",
        "dora_executable",
        "other_worktree_visible",
    ],
)
def test_native_sandbox_negative_probe_inspection_errors_fail_closed(monkeypatch, probe_key: str):
    """Native isolation design: inaccessible negative inspections are invalid, never safe."""
    from aisle.harness.native_sandbox import sandbox_probe_command, verify_sandbox_probe

    source = sandbox_probe_command()[3]
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    if probe_key == "host_process_visible":
        original_stat = os.stat

        def failing_stat(path, *args, **kwargs):
            if str(path) == "/proc/self/ns/pid":
                raise OSError("fixture inaccessible proc")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr("os.stat", failing_stat)
    elif probe_key == "nvidia_visible":
        monkeypatch.setattr(
            Path,
            "iterdir",
            lambda self: (_ for _ in ()).throw(OSError("fixture inaccessible dev")),
        )
    elif probe_key in {"docker_socket_visible", "other_worktree_visible"}:
        original_stat = Path.stat
        fault_paths = {
            "docker_socket_visible": {
                "/var/run/docker.sock",
                "/run/docker.sock",
                "/run/containerd/containerd.sock",
            },
            "other_worktree_visible": {
                "/.worktrees",
                "/repo/.worktrees",
                "/worktrees",
                "/workspace/../.worktrees",
            },
        }[probe_key]

        def failing_stat(path: Path, *args, **kwargs):
            if str(path) in fault_paths:
                raise OSError(f"fixture inaccessible {probe_key}")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", failing_stat)
    elif probe_key == "genesis_importable":
        monkeypatch.setattr(
            "importlib.util.find_spec",
            lambda name: (_ for _ in ()).throw(OSError("fixture inaccessible import path")),
        )
    else:
        original_stat = Path.stat

        def failing_executable_stat(path: Path, *args, **kwargs):
            if path.name == "dora":
                raise OSError("fixture inaccessible executable path")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", failing_executable_stat)

    result = _execute_native_sandbox_probe(source)
    ok, errors = verify_sandbox_probe(result)

    assert result[probe_key] is None
    assert ok is False
    assert any(probe_key in error for error in errors)


def test_native_sandbox_negative_probes_distinguish_successful_absence(monkeypatch):
    """Native isolation design: a completed absence inspection remains the exact false boolean."""
    from aisle.harness.native_sandbox import sandbox_probe_command

    host_namespace = os.stat("/proc/self/ns/pid").st_ino
    source = sandbox_probe_command()[3].replace(
        f"HOST_PID_NAMESPACE = {host_namespace}",
        f"HOST_PID_NAMESPACE = {host_namespace + 1}",
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(()))
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)

    result = _execute_native_sandbox_probe(source)

    assert {
        key: result[key]
        for key in (
            "host_process_visible",
            "nvidia_visible",
            "docker_socket_visible",
            "genesis_importable",
            "dora_executable",
            "other_worktree_visible",
        )
    } == {
        "host_process_visible": False,
        "nvidia_visible": False,
        "docker_socket_visible": False,
        "genesis_importable": False,
        "dora_executable": False,
        "other_worktree_visible": False,
    }


def _sandbox_probe_with_import_locations(source: str, locations: tuple[object, ...]) -> str:
    marker = "HOST_PID_NAMESPACE ="
    controlled = source.replace(
        marker,
        f"IMPORT_SEARCH_LOCATIONS = {locations!r}\n\n{marker}",
        1,
    )
    assert controlled != source
    return controlled


def test_native_sandbox_genesis_probe_rejects_unreadable_import_directory(
    tmp_path: Path, monkeypatch
):
    """Native isolation design: FileFinder-suppressed PermissionError remains invalid."""
    from aisle.harness.native_sandbox import sandbox_probe_command, verify_sandbox_probe

    unreadable = tmp_path / "unreadable-import-root"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    source = _sandbox_probe_with_import_locations(sandbox_probe_command()[3], (str(unreadable),))
    original_scandir = os.scandir
    find_spec_calls: list[str] = []

    def permission_denied(path):
        if Path(path) == unreadable:
            raise PermissionError("fixture unreadable import directory")
        return original_scandir(path)

    monkeypatch.setattr("os.scandir", permission_denied)
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: find_spec_calls.append(name) or None,
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])

    try:
        result = _execute_native_sandbox_probe(source)
        ok, errors = verify_sandbox_probe(result)
    finally:
        unreadable.chmod(0o700)

    assert result["genesis_importable"] is None
    assert find_spec_calls == []
    assert ok is False
    assert any("genesis_importable" in error for error in errors)


@pytest.mark.parametrize("malformed_location", [None, "", "ordinary-file"])
def test_native_sandbox_genesis_probe_rejects_malformed_import_locations(
    tmp_path: Path, monkeypatch, malformed_location: object
):
    """Native isolation design: non-path, empty, and non-archive files fail closed."""
    from aisle.harness.native_sandbox import sandbox_probe_command

    if malformed_location == "ordinary-file":
        location: object = str(tmp_path / "not-an-import-archive")
        Path(location).write_text("not a zip archive\n")
    else:
        location = malformed_location
    source = _sandbox_probe_with_import_locations(sandbox_probe_command()[3], (location,))
    find_spec_calls: list[str] = []
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: find_spec_calls.append(name) or None,
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])

    result = _execute_native_sandbox_probe(source)

    assert result["genesis_importable"] is None
    assert find_spec_calls == []


def test_native_sandbox_genesis_probe_allows_confirmed_nonexistent_import_path(
    tmp_path: Path, monkeypatch
):
    """Native isolation design: a stat-confirmed missing search entry cannot hide Genesis."""
    from aisle.harness.native_sandbox import sandbox_probe_command

    missing = tmp_path / "missing-python-zip"
    source = _sandbox_probe_with_import_locations(sandbox_probe_command()[3], (str(missing),))
    original_stat = Path.stat
    inspected: list[Path] = []
    find_spec_calls: list[str] = []

    def missing_stat(path: Path, *args, **kwargs):
        if path == missing:
            inspected.append(path)
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", missing_stat)
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: find_spec_calls.append(name) or None,
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])

    result = _execute_native_sandbox_probe(source)

    assert inspected == [missing]
    assert find_spec_calls == ["genesis"]
    assert result["genesis_importable"] is False
