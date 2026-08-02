"""Shared deliberately incomplete behavior for both S1 ablation starters."""

from __future__ import annotations


def _command(kind: str, payload: dict) -> dict:
    return {"kind": kind, "payload": payload}


class S1StarterStateMachine:
    """Issue one pick-and-deliver attempt from a deterministic typed plan.

    The complete order is recorded for treatment parity, but the starter
    intentionally acts on only its first target. Navigation completion advances
    the sequence without grasp confirmation, retry, or recovery.
    """

    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        self.seed = seed
        self.initial_quantities: tuple[tuple[str, int], ...] = ()
        self.target_order: tuple[str, ...] = ()
        self._first_zone: str | None = None
        self._first_product_id: str | None = None
        self._phase = "idle"

    def start(self, order: dict, subtask_plan: dict) -> list[dict]:
        raw_order = order.get("order", []) if isinstance(order, dict) else []
        raw_subtasks = subtask_plan.get("subtasks", []) if isinstance(subtask_plan, dict) else []
        if not isinstance(raw_order, list) or not isinstance(raw_subtasks, list):
            raise ValueError("order and subtask plan must contain lists")

        quantities: list[tuple[str, int]] = []
        for line in raw_order:
            if not isinstance(line, dict):
                raise ValueError("order lines must be objects")
            product, qty = line.get("product"), line.get("qty")
            if (
                not isinstance(product, str)
                or not product
                or isinstance(qty, bool)
                or not isinstance(qty, int)
                or qty < 1
            ):
                raise ValueError("order lines require product and positive integer qty")
            quantities.append((product, qty))

        targets: list[str] = []
        first_zone: str | None = None
        preceding_location: str | None = None
        for subtask in raw_subtasks:
            if not isinstance(subtask, dict):
                raise ValueError("subtasks must be objects")
            if subtask.get("op") == "goto":
                location = subtask.get("location")
                preceding_location = location if isinstance(location, str) else None
            elif subtask.get("op") == "pick":
                slot = subtask.get("slot")
                if not isinstance(slot, str) or not slot:
                    raise ValueError("starter pick subtasks require a slot")
                targets.append(f"{slot}#0")
                if first_zone is None:
                    first_zone = preceding_location

        self.initial_quantities = tuple(quantities)
        self.target_order = tuple(targets)
        if not targets:
            self._phase = "done"
            return []
        if not first_zone:
            raise ValueError("first starter pick has no preceding navigation target")
        self._first_zone = first_zone
        self._first_product_id = targets[0]
        self._phase = "shelf_nav"
        return [_command("nav_goal", {"location": first_zone})]

    def on_nav_result(self, _result: dict) -> list[dict]:
        if self._phase == "shelf_nav":
            self._phase = "counter_nav"
            return [
                _command(
                    "gripper_cmd",
                    {"action": "close", "product_id": self._first_product_id},
                ),
                _command("nav_goal", {"location": "counter"}),
            ]
        if self._phase == "counter_nav":
            self._phase = "done"
            return [
                _command(
                    "gripper_cmd",
                    {"action": "open", "product_id": self._first_product_id},
                )
            ]
        return []


def canonical_action_intent(events: list[dict]) -> list[dict]:
    """Normalize starter command records into representation-neutral intent."""
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    normalized: list[dict] = []
    for event in events:
        if not isinstance(event, dict) or set(event) != {"kind", "payload"}:
            raise ValueError("invalid starter command record")
        kind, payload = event["kind"], event["payload"]
        if not isinstance(payload, dict):
            raise ValueError("invalid starter command payload")
        if kind == "nav_goal":
            location = payload.get("location")
            if not isinstance(location, str) or not location:
                raise ValueError("invalid starter navigation target")
            normalized.append({"action": "nav", "target": location})
            continue
        if kind == "gripper_cmd":
            action, product_id = payload.get("action"), payload.get("product_id")
            if action not in ("close", "open") or not isinstance(product_id, str):
                raise ValueError("invalid starter gripper intent")
            normalized.append(
                {
                    "action": "pick" if action == "close" else "place",
                    "target": product_id,
                }
            )
            continue
        raise ValueError(f"unsupported starter command kind {kind!r}")
    return normalized
