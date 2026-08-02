"""Pure input and output contract for editable S1 script policies."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

PolicyCommandKind = Literal["nav_goal", "joint_cmd", "gripper_cmd"]
_COMMAND_KINDS = frozenset({"nav_goal", "joint_cmd", "gripper_cmd"})


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


@dataclass(frozen=True)
class PolicyEvent:
    """One normalized simulator observation delivered to a script policy."""

    kind: str
    payload: dict
    sim_time_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("event kind must be a non-empty string")
        if not isinstance(self.payload, dict) or not _is_json_value(self.payload):
            raise ValueError("event payload must be JSON-compatible")
        if isinstance(self.sim_time_ns, bool) or not isinstance(self.sim_time_ns, int):
            raise ValueError("event sim_time_ns must be an integer")
        if self.sim_time_ns < 0:
            raise ValueError("event sim_time_ns must be non-negative")


@dataclass(frozen=True)
class PolicyCommand:
    """One bounded command request emitted by a script policy."""

    kind: PolicyCommandKind
    payload: list[float] | dict

    def __post_init__(self) -> None:
        if self.kind not in _COMMAND_KINDS:
            raise ValueError("command kind must be nav_goal, joint_cmd, or gripper_cmd")
        if isinstance(self.payload, list):
            if not all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and isfinite(float(value))
                for value in self.payload
            ):
                raise ValueError("command list payload must contain finite numbers")
            return
        if not isinstance(self.payload, dict) or not _is_json_value(self.payload):
            raise ValueError("command payload must be a JSON-compatible dict or float list")
