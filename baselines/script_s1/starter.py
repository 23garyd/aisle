"""Intentionally incomplete, editable S1 script-policy starter."""

from __future__ import annotations

from baselines.script_s1.contract import PolicyCommand, PolicyEvent


class StarterPolicy:
    """Issue a single ordered item navigation/pick/delivery sequence."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.ordered_items: list[str] = []

    def on_event(self, event: PolicyEvent) -> list[PolicyCommand]:
        if event.kind != "episode_goal":
            return []
        raw_items = event.payload.get("items", [])
        if isinstance(raw_items, list):
            self.ordered_items = [item for item in raw_items if isinstance(item, str)]
        item = self.ordered_items[0] if self.ordered_items else "requested_item"
        return [
            PolicyCommand("nav_goal", {"target": item}),
            PolicyCommand("gripper_cmd", {"action": "close"}),
            PolicyCommand("nav_goal", {"target": "delivery_tray"}),
        ]


def create_policy(seed: int) -> StarterPolicy:
    """Create the deterministic starter policy for one rollout seed."""
    return StarterPolicy(seed)
