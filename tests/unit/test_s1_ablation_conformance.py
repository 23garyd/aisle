"""Starter-revision conformance for the S1 harness-versus-script ablation."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

pytestmark = pytest.mark.unit


def _goal() -> dict:
    return {
        "scenario": "S1",
        "seed": 17,
        "order": [
            {"product": "ibuprofen", "spec": "55x35x90 mm box", "qty": 2},
            {"product": "amoxicillin", "spec": "60x40x100 mm box", "qty": 1},
        ],
    }


def _plan(goal: dict) -> dict:
    from aisle.nodes.task_planner import plan_subtasks
    from aisle.scenes.store import load_planogram

    return {"subtasks": plan_subtasks(goal, load_planogram())}


def _script_events(goal: dict) -> tuple[object, list[dict]]:
    from baselines.script_s1.contract import PolicyEvent
    from baselines.script_s1.starter import create_policy

    policy = create_policy(goal["seed"])
    commands = []
    for event in (
        PolicyEvent("episode_goal", goal, 1),
        PolicyEvent("poses", {"values": [0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0]}, 2),
        PolicyEvent("nav_result", {"status": "success"}, 3),
        PolicyEvent("nav_result", {"status": "success"}, 4),
    ):
        commands.extend(asdict(command) for command in policy.on_event(event))
    return policy, commands


def _aisle_events(goal: dict) -> tuple[object, list[dict]]:
    from aisle.nodes.order_reader import read_order
    from aisle.nodes.s1_ablation_driver import S1AblationDriver

    driver = S1AblationDriver(goal["seed"])
    commands = []
    for kind, payload in (
        ("subtask_plan", _plan(goal)),
        ("order", read_order(goal)),
        ("poses", {"values": [0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0]}),
        ("nav_result", {"status": "success"}),
        ("nav_result", {"status": "success"}),
    ):
        commands.extend(driver.on_event(kind, payload))
    return driver, commands


def test_starter_representations_emit_the_same_initial_action_intent():
    """CON-5: both starter forms normalize to the same fixed S1 action sequence."""
    from aisle.harness.s1_ablation_common import canonical_action_intent

    goal = _goal()
    script, script_events = _script_events(goal)
    aisle, aisle_events = _aisle_events(goal)
    expected = [
        {"action": "nav", "target": "shelf_zone_A"},
        {"action": "pick", "target": "A1-L1-S0#0"},
        {"action": "nav", "target": "counter"},
        {"action": "place", "target": "A1-L1-S0#0"},
    ]

    assert canonical_action_intent(script_events) == expected
    assert canonical_action_intent(aisle_events) == expected
    assert script.seed == aisle.seed == 17


def test_starter_representations_preserve_quantities_and_target_order():
    """CON-5, RS-3: both forms preserve ordered quantities and deterministic targets."""
    goal = _goal()
    script, _ = _script_events(goal)
    aisle, _ = _aisle_events(goal)

    assert (
        script.initial_quantities
        == aisle.initial_quantities
        == (
            ("ibuprofen", 2),
            ("amoxicillin", 1),
        )
    )
    assert (
        script.target_order
        == aisle.target_order
        == (
            "A1-L1-S0#0",
            "A1-L1-S1#0",
            "B1-L1-S0#0",
        )
    )


def test_starter_deliberately_advances_without_nav_or_grasp_recovery():
    """HAR-3: starter revision has no retry, recovery, or grasp confirmation."""
    from aisle.harness.s1_ablation_common import canonical_action_intent
    from aisle.nodes.order_reader import read_order
    from aisle.nodes.s1_ablation_driver import S1AblationDriver

    goal = _goal()
    driver = S1AblationDriver(goal["seed"])
    commands = []
    commands.extend(driver.on_event("order", read_order(goal)))
    commands.extend(driver.on_event("subtask_plan", _plan(goal)))
    commands.extend(driver.on_event("nav_result", {"status": "failed"}))

    assert canonical_action_intent(commands) == [
        {"action": "nav", "target": "shelf_zone_A"},
        {"action": "pick", "target": "A1-L1-S0#0"},
        {"action": "nav", "target": "counter"},
    ]


def test_canonical_action_intent_rejects_nonstarter_command_shapes():
    """CON-8: malformed or unrelated events cannot be misreported as starter intent."""
    from aisle.harness.s1_ablation_common import canonical_action_intent

    with pytest.raises(ValueError, match="starter command"):
        canonical_action_intent([{"kind": "joint_cmd", "payload": [0.0] * 9}])


def test_script_runtime_accepts_labeled_semantic_pick_and_place_commands():
    """BG-1: product labels survive policy intent while the guard wire stays scalar."""
    from aisle.nodes.script_s1_runtime import prepare_commands
    from baselines.script_s1.contract import PolicyCommand

    prepared = prepare_commands(
        [
            PolicyCommand(
                "gripper_cmd",
                {"action": "close", "product_id": "A1-L1-S0#0"},
            ),
            PolicyCommand(
                "gripper_cmd",
                {"action": "open", "product_id": "A1-L1-S0#0"},
            ),
        ]
    )

    assert [command.payload for command in prepared] == [[1.0], [0.0]]


def test_typed_driver_is_in_the_fixed_orphan_reaper():
    """CON-7: typed starter cleanup includes its fixed behavioral driver."""
    from aisle.harness.reaper import NODE_PATTERNS

    assert "nodes/s1_ablation_driver.py" in NODE_PATTERNS
