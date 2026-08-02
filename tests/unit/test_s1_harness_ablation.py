"""Unit tests for the S1 harness-versus-script neutral attempt schema."""

import sys
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
