"""Typed dora adapter for the shared deliberately incomplete S1 starter."""

from __future__ import annotations

import json
import os

from aisle.harness.s1_ablation_common import S1StarterStateMachine


class S1AblationDriver:
    """Join typed order/plan results and adapt shared intents to dora outputs."""

    def __init__(self, seed: int) -> None:
        self._machine = S1StarterStateMachine(seed)
        self._order: dict | None = None
        self._plan: dict | None = None
        self._started = False

    @property
    def seed(self) -> int:
        return self._machine.seed

    @property
    def initial_quantities(self) -> tuple[tuple[str, int], ...]:
        return self._machine.initial_quantities

    @property
    def target_order(self) -> tuple[str, ...]:
        return self._machine.target_order

    def on_event(self, kind: str, payload: dict) -> list[dict]:
        if kind == "order":
            self._order = payload
        elif kind == "subtask_plan":
            self._plan = payload
        elif kind == "nav_result":
            return self._machine.on_nav_result(payload)
        if not self._started and self._order is not None and self._plan is not None:
            self._started = True
            return self._machine.start(self._order, self._plan)
        return []


def main() -> None:
    import numpy as np
    import pyarrow as pa
    from dora import Node

    from aisle.topics import make_sender

    node = Node()
    send = make_sender(node)
    driver = S1AblationDriver(int(os.environ.get("AISLE_SEED", "0")))
    nav_seq = 0

    for event in node:
        if event["type"] != "INPUT":
            continue
        kind = event["id"]
        metadata = event.get("metadata") or {}
        if kind == "reset_done":
            driver = S1AblationDriver(int(metadata.get("seed", driver.seed)))
            continue
        if kind in {"order", "subtask_plan", "nav_result"}:
            payload = json.loads(event["value"][0].as_py())
        else:
            payload = {"values": event["value"].to_numpy(zero_copy_only=False).reshape(-1).tolist()}
        for command in driver.on_event(kind, payload):
            if command["kind"] == "nav_goal":
                nav_seq += 1
                send(
                    "nav_goal",
                    pa.array([json.dumps(command["payload"])]),
                    {"goal_id": f"ablation-nav-{nav_seq:04d}"},
                )
            elif command["kind"] == "gripper_cmd":
                action = command["payload"]["action"]
                value = np.array([1.0 if action == "close" else 0.0], dtype=np.float32)
                send("gripper_cmd", pa.array(value), metadata)


if __name__ == "__main__":
    main()
