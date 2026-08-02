"""Fixed dora boundary for one editable S1 script policy.

The candidate receives only normalized ``PolicyEvent`` values. Commands are
validated as a complete batch before any output is sent, then serialized onto
the same guarded arm/navigation topics used by the S1 expert graph. Numeric
requests are shape-checked, not safety-clamped here: the unchanged
``budget-guard`` remains the sole safety authority.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from importlib import import_module, util
from io import StringIO
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    # Trusted fixed API only. The candidate directory is deliberately never
    # added to sys.path.
    sys.path.insert(0, str(_REPO_ROOT))

_contract = import_module("baselines.script_s1.contract")
PolicyCommand = _contract.PolicyCommand
PolicyEvent = _contract.PolicyEvent

_JSON_INPUTS = frozenset({"episode_goal", "nav_result"})
_NUMERIC_INPUTS = frozenset({"poses", "joint_state", "base_pose", "reset_done"})
_INPUTS = _JSON_INPUTS | _NUMERIC_INPUTS
_JOINT_DOF = 9


class CommandInvalid(RuntimeError):
    """Stable runtime termination for an invalid candidate command batch."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"COMMAND_INVALID: {detail}")


class PolicyInvalid(RuntimeError):
    """Candidate import/factory/handler failure after successful preflight."""


@dataclass(frozen=True)
class PreparedCommand:
    """A validated command ready for exact dora-topic serialization."""

    kind: str
    payload: list[float] | dict


def policy_event_from_dora(kind: str, value, metadata: dict | None) -> PolicyEvent:
    """Translate one declared dora input into the stable script contract."""
    if kind not in _INPUTS:
        raise ValueError(f"undeclared script runtime input {kind!r}")
    if kind in _JSON_INPUTS:
        payload = json.loads(value[0].as_py())
        if not isinstance(payload, dict):
            raise ValueError(f"{kind} must carry one JSON object")
    else:
        payload = {"values": value.to_numpy(zero_copy_only=False).reshape(-1).tolist()}
    raw_time = (metadata or {}).get("sim_time_ns", 0)
    if isinstance(raw_time, bool):
        raise ValueError("sim_time_ns must be an integer")
    return PolicyEvent(kind=kind, payload=payload, sim_time_ns=int(raw_time))


def prepare_commands(commands: object) -> list[PreparedCommand]:
    """Validate a whole candidate batch before returning any emissions.

    Safety-range violations intentionally survive unchanged for budget-guard.
    Only the declared transport shapes are enforced here.
    """
    if not isinstance(commands, list):
        raise CommandInvalid("on_event must return a list")

    prepared: list[PreparedCommand] = []
    for command in commands:
        if not isinstance(command, PolicyCommand):
            raise CommandInvalid("every result must be a PolicyCommand")
        if command.kind == "nav_goal":
            if not isinstance(command.payload, dict):
                raise CommandInvalid("nav_goal payload must be a dict")
            prepared.append(PreparedCommand(command.kind, dict(command.payload)))
        elif command.kind == "joint_cmd":
            if not isinstance(command.payload, list) or len(command.payload) != _JOINT_DOF:
                raise CommandInvalid(f"joint_cmd payload must be a {_JOINT_DOF}-value list")
            prepared.append(PreparedCommand(command.kind, list(command.payload)))
        elif command.kind == "gripper_cmd":
            payload = command.payload
            if isinstance(payload, list) and len(payload) == 1:
                prepared.append(PreparedCommand(command.kind, list(payload)))
            elif isinstance(payload, dict) and set(payload) == {"action"}:
                action = payload["action"]
                if action not in ("open", "close"):
                    raise CommandInvalid("gripper action must be open or close")
                prepared.append(PreparedCommand(command.kind, [0.0 if action == "open" else 1.0]))
            else:
                raise CommandInvalid(
                    "gripper_cmd payload must be a one-value list or an open/close action"
                )
        else:  # defensive if a forged object bypasses PolicyCommand.__post_init__
            raise CommandInvalid(f"undeclared command kind {command.kind!r}")
    return prepared


def _load_factory(path: Path):
    if not path.is_absolute() or not path.is_file():
        raise PolicyInvalid("AISLE_SCRIPT_POLICY must name an absolute policy file")
    spec = util.spec_from_file_location("aisle_script_candidate", path)
    if spec is None or spec.loader is None:
        raise PolicyInvalid("candidate policy has no import loader")
    module = util.module_from_spec(spec)
    with redirect_stdout(StringIO()):
        spec.loader.exec_module(module)
    factory = getattr(module, "create_policy", None)
    if not callable(factory):
        raise PolicyInvalid("candidate policy has no callable create_policy")
    return factory


def _create_policy(factory, seed: int):
    with redirect_stdout(StringIO()):
        policy = factory(seed)
    if not callable(getattr(policy, "on_event", None)):
        raise PolicyInvalid("create_policy returned no callable on_event")
    return policy


def _call_policy(policy, event: PolicyEvent) -> list[PreparedCommand]:
    try:
        with redirect_stdout(StringIO()):
            commands = policy.on_event(event)
    except ValueError as exc:
        raise CommandInvalid(str(exc)) from exc
    except Exception as exc:
        raise PolicyInvalid(f"policy on_event failed: {type(exc).__name__}") from exc
    return prepare_commands(commands)


def main() -> None:
    import numpy as np
    import pyarrow as pa
    from dora import Node

    from aisle.topics import make_sender

    path = Path(os.environ.get("AISLE_SCRIPT_POLICY", ""))
    try:
        seed = int(os.environ["AISLE_SEED"])
        factory = _load_factory(path)
        policy = _create_policy(factory, seed)
    except (KeyError, ValueError, OSError, PolicyInvalid) as exc:
        raise SystemExit(f"POLICY_INVALID: {exc}") from exc

    node = Node()
    send = make_sender(node)
    nav_seq = 0

    for raw_event in node:
        if raw_event["type"] != "INPUT":
            continue
        metadata = raw_event.get("metadata") or {}
        try:
            event = policy_event_from_dora(raw_event["id"], raw_event["value"], metadata)
            if event.kind == "reset_done":
                reset_seed = metadata.get("seed", seed)
                if isinstance(reset_seed, bool):
                    raise PolicyInvalid("reset seed must be an integer")
                policy = _create_policy(factory, int(reset_seed))
            event_record = {
                "kind": event.kind,
                "payload": event.payload,
                "sim_time_ns": event.sim_time_ns,
            }
            output_meta = {
                "env_id": int(metadata.get("env_id", 0)),
                "sim_time_ns": event.sim_time_ns,
            }
            send(
                "policy_event",
                pa.array([json.dumps(event_record, sort_keys=True, separators=(",", ":"))]),
                output_meta,
            )
            commands = _call_policy(policy, event)
            for command in commands:
                if command.kind == "nav_goal":
                    nav_seq += 1
                    send(
                        "nav_goal",
                        pa.array(
                            [json.dumps(command.payload, sort_keys=True, separators=(",", ":"))]
                        ),
                        {**output_meta, "goal_id": f"script-nav-{nav_seq:04d}"},
                    )
                else:
                    send(
                        command.kind,
                        pa.array(np.asarray(command.payload, dtype=np.float32)),
                        output_meta,
                    )
        except CommandInvalid as exc:
            raise SystemExit(str(exc)) from exc
        except PolicyInvalid as exc:
            raise SystemExit(f"POLICY_INVALID: {exc}") from exc


if __name__ == "__main__":
    main()
