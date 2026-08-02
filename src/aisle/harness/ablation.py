"""Neutral result records for the S1 harness-versus-script ablation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

_ATTEMPT_KEYS = frozenset(
    {
        "attempt_id",
        "candidate_hash",
        "preflight",
        "episodes",
        "failures",
        "safety",
        "timing",
        "artifacts",
    }
)
_PREFLIGHT_KEYS = frozenset({"ok", "errors", "wall_s"})
_SAFETY_KEYS = frozenset({"ungated", "clamps", "extra_item"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _require_exact_keys(value: object, expected: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dict")
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{name} is missing fields: {sorted(missing)}")
    return value


def _nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _string_dict(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dict")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{name} must map strings to strings")
    return dict(value)


def _count_dict(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dict")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return {key: _nonnegative_int(item, f"{name}.{key}") for key, item in value.items()}


def _timing_dict(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("timing must be a dict")
    if not all(isinstance(key, str) for key in value):
        raise ValueError("timing keys must be strings")
    return {key: _nonnegative_float(item, f"timing.{key}") for key, item in value.items()}


def _dict_list(value: object, name: str) -> tuple[dict, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be a list of dicts")
    return tuple(dict(item) for item in value)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[dict, ...]
    wall_s: float

    @classmethod
    def from_dict(cls, value: dict) -> PreflightResult:
        raw = _require_exact_keys(value, _PREFLIGHT_KEYS, "preflight")
        if not isinstance(raw["ok"], bool):
            raise ValueError("preflight.ok must be a bool")
        return cls(
            ok=raw["ok"],
            errors=_dict_list(raw["errors"], "preflight.errors"),
            wall_s=_nonnegative_float(raw["wall_s"], "preflight.wall_s"),
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [dict(error) for error in self.errors],
            "wall_s": self.wall_s,
        }


@dataclass(frozen=True)
class SafetyResult:
    ungated: int
    clamps: int
    extra_item: int

    @classmethod
    def from_dict(cls, value: dict) -> SafetyResult:
        raw = _require_exact_keys(value, _SAFETY_KEYS, "safety")
        return cls(
            ungated=_nonnegative_int(raw["ungated"], "safety.ungated"),
            clamps=_nonnegative_int(raw["clamps"], "safety.clamps"),
            extra_item=_nonnegative_int(raw["extra_item"], "safety.extra_item"),
        )

    def to_dict(self) -> dict:
        return {"ungated": self.ungated, "clamps": self.clamps, "extra_item": self.extra_item}


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    candidate_hash: str
    preflight: PreflightResult
    episodes: tuple[dict, ...]
    failures: dict[str, int]
    safety: SafetyResult
    timing: dict[str, float]
    artifacts: dict[str, str]

    @classmethod
    def from_dict(cls, value: dict) -> AttemptResult:
        raw = _require_exact_keys(value, _ATTEMPT_KEYS, "attempt")
        if not isinstance(raw["attempt_id"], str) or not raw["attempt_id"]:
            raise ValueError("attempt_id must be a non-empty string")
        if not isinstance(raw["candidate_hash"], str) or not _SHA256_RE.fullmatch(
            raw["candidate_hash"]
        ):
            raise ValueError("candidate_hash must be a lowercase SHA-256 hex digest")
        return cls(
            attempt_id=raw["attempt_id"],
            candidate_hash=raw["candidate_hash"],
            preflight=PreflightResult.from_dict(raw["preflight"]),
            episodes=_dict_list(raw["episodes"], "episodes"),
            failures=_count_dict(raw["failures"], "failures"),
            safety=SafetyResult.from_dict(raw["safety"]),
            timing=_timing_dict(raw["timing"]),
            artifacts=_string_dict(raw["artifacts"], "artifacts"),
        )

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "candidate_hash": self.candidate_hash,
            "preflight": self.preflight.to_dict(),
            "episodes": [dict(episode) for episode in self.episodes],
            "failures": dict(self.failures),
            "safety": self.safety.to_dict(),
            "timing": dict(self.timing),
            "artifacts": dict(self.artifacts),
        }
