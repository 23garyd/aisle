"""Controlled neutral events for adapter-level S1 starter conformance."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
from dora import Node

GOAL = {
    "scenario": "S1",
    "seed": 17,
    "order": [
        {"product": "ibuprofen", "spec": "55x35x90 mm box", "qty": 2},
        {"product": "amoxicillin", "spec": "60x40x100 mm box", "qty": 1},
    ],
}
PLAN = {
    "subtasks": [
        {"op": "goto", "location": "shelf_zone_A"},
        {"op": "pick", "category": "ibuprofen", "slot": "A1-L1-S0"},
        {"op": "goto", "location": "counter"},
        {"op": "place", "where": "counter"},
        {"op": "goto", "location": "shelf_zone_A"},
        {"op": "pick", "category": "ibuprofen", "slot": "A1-L1-S1"},
        {"op": "goto", "location": "counter"},
        {"op": "place", "where": "counter"},
        {"op": "goto", "location": "shelf_zone_B"},
        {"op": "pick", "category": "amoxicillin", "slot": "B1-L1-S0"},
        {"op": "goto", "location": "counter"},
        {"op": "place", "where": "counter"},
    ]
}


def main() -> None:
    node = Node()
    tick = 0
    for event in node:
        if event["type"] != "INPUT":
            continue
        tick += 1
        if tick == 1:
            metadata = {"seed": 17, "sim_time_ns": 1}
            node.send_output(
                "reset_done",
                pa.array(np.array([1], dtype=np.uint32)),
                metadata=metadata,
            )
            node.send_output(
                "episode_goal",
                pa.array([json.dumps(GOAL)]),
                metadata=metadata,
            )
            node.send_output(
                "order",
                pa.array([json.dumps({"order": GOAL["order"], "seed": 17})]),
                metadata=metadata,
            )
            node.send_output(
                "subtask_plan",
                pa.array([json.dumps(PLAN)]),
                metadata=metadata,
            )
            node.send_output(
                "poses",
                pa.array(np.array([0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0], dtype=np.float32)),
                metadata=metadata,
            )
            node.send_output(
                "joint_state",
                pa.array(np.zeros(9, dtype=np.float32)),
                metadata=metadata,
            )
            node.send_output(
                "base_pose",
                pa.array(np.zeros(3, dtype=np.float32)),
                metadata=metadata,
            )
        elif tick in (10, 20):
            node.send_output(
                "nav_result",
                pa.array([json.dumps({"status": "success"})]),
                metadata={"sim_time_ns": tick},
            )


if __name__ == "__main__":
    main()
