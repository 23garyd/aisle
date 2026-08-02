"""Intentionally incomplete, editable S1 script-policy starter."""

from __future__ import annotations

from aisle.harness.s1_ablation_common import S1StarterStateMachine
from aisle.nodes.order_reader import read_order
from aisle.nodes.task_planner import plan_subtasks
from aisle.scenes.store import load_planogram
from baselines.script_s1.contract import PolicyCommand, PolicyEvent


class StarterPolicy:
    """Adapt the common starter behavior to the monolithic policy contract."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.ordered_items: list[str] = []
        self._machine = S1StarterStateMachine(seed)

    @property
    def initial_quantities(self) -> tuple[tuple[str, int], ...]:
        return self._machine.initial_quantities

    @property
    def target_order(self) -> tuple[str, ...]:
        return self._machine.target_order

    def on_event(self, event: PolicyEvent) -> list[PolicyCommand]:
        intents: list[dict]
        if event.kind == "episode_goal":
            if not isinstance(event.payload.get("order"), list):
                return []
            order = read_order(event.payload)
            self.ordered_items = [line["product"] for line in order["order"]]
            plan = {
                "subtasks": plan_subtasks(event.payload, load_planogram()),
            }
            intents = self._machine.start(order, plan)
        elif event.kind == "nav_result":
            intents = self._machine.on_nav_result(event.payload)
        else:
            return []
        return [PolicyCommand(intent["kind"], intent["payload"]) for intent in intents]


def create_policy(seed: int) -> StarterPolicy:
    """Create the deterministic starter policy for one rollout seed."""
    return StarterPolicy(seed)
