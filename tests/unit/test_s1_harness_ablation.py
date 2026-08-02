"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import json
import signal
import subprocess
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

    result = script_rollout.run_script_rollout(policy, "3", "script-unit")

    assert result.attempt_id == "script-unit"
    assert result.episodes == (_episode_record(0, 3),)
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
            candidate_hash="0" * 64,
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

    assert result.failures == {"INFRA_SAFETY_UNAVAILABLE": 1}


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
    assert len(result.candidate_hash) == 64


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
