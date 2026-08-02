"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))


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
