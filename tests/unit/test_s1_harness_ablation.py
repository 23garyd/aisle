"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import json
import shutil
import signal
import subprocess
import sys
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

    def execute(command, cwd, agent, on_line, stop_reason, wall_ceiling_s):
        assert command and cwd.is_dir() and agent in {"claude", "codex"}
        assert wall_ceiling_s <= 4 * 3600
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

    assert returncode == 0
    assert response["stopped"] == "token_budget"
    assert response["tokens_spent"] == 500_000
    assert budget_remaining(session_dir / "worktree")["episodes_left"] == 40


def test_controller_scores_unavailable_safety_evidence_fail_closed(tmp_path: Path):
    """CON-5, CON-7: INFRA_SAFETY_UNAVAILABLE is not inferred as held-out zero."""
    from aisle.harness.ablation import AttemptResult, PreflightResult, SafetyResult
    from tools import s1_harness_ablation as controller

    class UnavailableSafetyAdapter:
        def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult:
            return AttemptResult(
                attempt_id=run_id,
                candidate_hash="b" * 64,
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
    from aisle.harness.ablation import AttemptResult, PreflightResult, SafetyResult
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
                candidate_hash="c" * 64,
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
