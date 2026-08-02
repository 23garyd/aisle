"""Neutral result records for the S1 harness-versus-script ablation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
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
_ABSENT_CANDIDATE_HASH = "0" * 64
_SENTINEL_CANDIDATE_IDENTITIES = frozenset({"absent", "invalid"})
_LEDGER_KEYS = frozenset({"seq", "prev_sha256", "event", "sha256"})


def paired_assignments(seed: int, pairs: int) -> list[str]:
    """Return seeded, pair-adjacent assignments for the two ablation arms."""
    if isinstance(pairs, bool) or not isinstance(pairs, int) or pairs < 0:
        raise ValueError("pairs must be a non-negative integer")

    rng = random.Random(seed)
    assignments: list[str] = []
    for _ in range(pairs):
        pair = ["aisle", "script"]
        rng.shuffle(pair)
        assignments.extend(pair)
    return assignments


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file's exact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ledger_hash(seq: int, prev_sha256: str | None, event: dict) -> str:
    payload = _canonical_json({"seq": seq, "prev_sha256": prev_sha256, "event": event}).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _parse_ledger(lines: list[str]) -> tuple[bool, str | None]:
    previous: str | None = None
    for expected_seq, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False, None
        if not isinstance(record, dict) or set(record) != _LEDGER_KEYS:
            return False, None
        seq = record["seq"]
        prev_sha256 = record["prev_sha256"]
        event = record["event"]
        entry_sha256 = record["sha256"]
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq != expected_seq
            or prev_sha256 != previous
            or not isinstance(event, dict)
            or not isinstance(entry_sha256, str)
            or not _SHA256_RE.fullmatch(entry_sha256)
        ):
            return False, None
        if entry_sha256 != _ledger_hash(seq, prev_sha256, event):
            return False, None
        previous = entry_sha256
    return True, previous


def append_ledger(path: Path, event: dict) -> str:
    """Append a canonical hash-chained event under an exclusive file lock."""
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as ledger:
        fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
        try:
            ledger.seek(0)
            lines = ledger.read().splitlines()
            valid, previous = _parse_ledger(lines)
            if not valid:
                raise ValueError("ledger verification failed before append")
            seq = len(lines) + 1
            entry_sha256 = _ledger_hash(seq, previous, event)
            record = {
                "seq": seq,
                "prev_sha256": previous,
                "event": event,
                "sha256": entry_sha256,
            }
            ledger.seek(0, os.SEEK_END)
            ledger.write(_canonical_json(record) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
            return entry_sha256
        finally:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)


def verify_ledger(path: Path) -> tuple[bool, str | None]:
    """Return whether a JSONL ledger verifies and its verified head hash."""
    if not path.exists():
        return True, None
    try:
        return _parse_ledger(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return False, None


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

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_hash, str) or not _SHA256_RE.fullmatch(
            self.candidate_hash
        ):
            raise ValueError("candidate_hash must be a lowercase SHA-256 hex digest")
        failures = _count_dict(self.failures, "failures")
        artifacts = _string_dict(self.artifacts, "artifacts")
        candidate_identity = artifacts.get("candidate_identity")
        if self.candidate_hash == _ABSENT_CANDIDATE_HASH:
            if candidate_identity not in _SENTINEL_CANDIDATE_IDENTITIES or failures != {
                "INFRA_ARGUMENT": 1
            }:
                raise ValueError(
                    "candidate_hash all-zero sentinel is reserved for invalid candidate input"
                )
        elif candidate_identity is not None:
            raise ValueError("candidate_identity requires the reserved all-zero candidate_hash")

    @classmethod
    def from_dict(cls, value: dict) -> AttemptResult:
        raw = _require_exact_keys(value, _ATTEMPT_KEYS, "attempt")
        if not isinstance(raw["attempt_id"], str) or not raw["attempt_id"]:
            raise ValueError("attempt_id must be a non-empty string")
        failures = _count_dict(raw["failures"], "failures")
        artifacts = _string_dict(raw["artifacts"], "artifacts")
        return cls(
            attempt_id=raw["attempt_id"],
            candidate_hash=raw["candidate_hash"],
            preflight=PreflightResult.from_dict(raw["preflight"]),
            episodes=_dict_list(raw["episodes"], "episodes"),
            failures=failures,
            safety=SafetyResult.from_dict(raw["safety"]),
            timing=_timing_dict(raw["timing"]),
            artifacts=artifacts,
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
