# Task 5 report — equivalent AISLE starter and conformance

## Status

Implemented the interface-equivalent typed AISLE starter and moved the common
starter-revision behavior into one pure state machine consumed by both
treatments. The full normalized synthetic sequence is:

```text
nav shelf_zone_A
pick A1-L1-S0#0
nav counter
place A1-L1-S0#0
```

The state machine records the complete initial order quantities and
deterministic target ordering, but deliberately acts on only the first target.
It has no grasp confirmation, retry, recovery, or robust quantity loop.

## RED evidence

The required focused conformance test was written first:

```text
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  uv run --no-sync pytest tests/unit/test_s1_ablation_conformance.py -q

4 failed
ModuleNotFoundError:
  aisle.harness.s1_ablation_common
  aisle.nodes.s1_ablation_driver
```

Typed graph and manifest tests were also RED before their artifacts existed:

```text
pytest tests/graph/test_s1_ablation_starters.py \
  tests/unit/test_manifests.py::test_registry_completeness -q

5 failed
FileNotFoundError: graphs/ablation_s1_starter.yaml
FileNotFoundError: registry/manifests/s1-ablation-driver.yaml
curated_core.toml drifted from the CAP-5 list
```

The bounded live run then exposed an orphan-cleanup omission. A focused
regression failed before the reaper was changed:

```text
pytest tests/unit/test_s1_ablation_conformance.py::\
test_typed_driver_is_in_the_fixed_orphan_reaper -q

1 failed
assert "nodes/s1_ablation_driver.py" in NODE_PATTERNS
```

## GREEN evidence

Focused unit conformance:

```text
pytest tests/unit/test_s1_ablation_conformance.py -q
5 passed in 0.15s
```

After the cleanup regression, the file contains six passing tests. The
combined worktree-pinned focused/registry/static-topology gate passed:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=<worktree>/src:<worktree> \
uv run --no-sync pytest \
  tests/unit/test_s1_ablation_conformance.py \
  tests/unit/test_s1_harness_ablation.py \
  tests/graph/test_s1_ablation_starters.py -m unit \
  tests/unit/test_manifests.py -q

49 passed, 64 deselected in 5.24s
```

The unpinned variant reported 28 manifests from the shared editable parent
checkout while the worktree contained 30. This is the already documented
no-sync editable-install limitation, so subprocess gates were rerun with only
trusted worktree code pinned; no candidate path was added.

The first full unit attempt then reached an existing corpus invariant that
Tasks 4–5 had made stale: the reserved-distribution registry mirror lacked
both `script-s1-runtime` and `s1-ablation-driver`. That precise RED was:

```text
test_reserved_root_mirrors_real_registry
1 failed, 638 passed, 1 skipped, 107 deselected
```

The minimal fixture manifests and source-existence stubs were added. The
focused mirror regression passed, followed by a fresh complete unit gate:

```text
pytest -m unit -q
639 passed, 1 skipped, 107 deselected in 214.08s
```

Final focused regressions after all fixes:

```text
test_s1_ablation_conformance.py + test_s1_harness_ablation.py
69 passed in 0.33s

typed static topology + manifests + reserved mirror
44 passed, 1 live test deselected in 5.32s
```

Graph and registry validation:

```text
harness validate graphs/ablation_s1_starter.yaml \
  --root . --embodiment mobile
{"ok": true, "errors": [], "warnings": []}

python -m aisle.harness.registry lint --root .
{"ok": true, "checked": 30, "errors": [], "warnings": []}

ruff format --check .
160 files already formatted

ruff check .
All checks passed

python tools/trace_check.py --root .
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

## Bounded live conformance

One graph test launched the typed and script starters sequentially on seed 0,
with an independent 480-second cap per arm. Each arm stopped as soon as the
goal, a post-reset oracle row, and first navigation intent were persisted. It
did not wait for the frozen verifier's 600-sim-second terminal deadline.

```text
pytest tests/graph/test_s1_ablation_starters.py::\
test_live_same_seed_starters_share_initial_state_and_intent -q -s

1 passed in 624.70s
```

Both arms produced:

```json
{
  "seed": 0,
  "goal_hash": "92d28f9d2f3687be264c65abaacebc38d1493b01b721296eeef4af1807f39026",
  "oracle_digest": "5ddc803bfd481868a0f19c63b4a7eadf36dadf807403a5265062d57f8fab8e38",
  "nav_goal": {"location": "shelf_zone_A"}
}
```

The live test also compares the fixed budget-guard environment and the exact
verifier node configuration. The terminal verifier result schema is compared
through the identical fixed verifier node/manifest boundary, not a live
episode result; waiting for such a result would have rerun the prohibited
hours-long incomplete-starter verifier path. The complete four-intent sequence,
initial quantities, target order, and seed handling are covered by the pure
unit conformance test.

The first live cleanup left only the new typed driver layers, revealing the
missing reaper entry. After the RED-to-GREEN reaper fix, those exact
task-owned processes were reaped, and a final process audit found no dora,
Genesis, typed-driver, or script-runtime workload.

## Files

Created:

- `src/aisle/harness/s1_ablation_common.py`
- `src/aisle/nodes/s1_ablation_driver.py`
- `graphs/ablation_s1_starter.yaml`
- `registry/manifests/s1-ablation-driver.yaml`
- `tests/unit/test_s1_ablation_conformance.py`
- `tests/graph/test_s1_ablation_starters.py`
- `tests/fixtures/roots/reserved_dists/registry/manifests/s1-ablation-driver.yaml`
- `tests/fixtures/roots/reserved_dists/registry/manifests/script-s1-runtime.yaml`
- `tests/fixtures/roots/reserved_dists/src/aisle/nodes/s1_ablation_driver.py`
- `tests/fixtures/roots/reserved_dists/src/aisle/nodes/script_s1_runtime.py`

Modified:

- `baselines/script_s1/starter.py`
- `src/aisle/nodes/script_s1_runtime.py`
- `src/aisle/harness/reaper.py`
- `registry/schema/curated_core.toml`
- `tests/unit/test_manifests.py`

No specification, scene, verifier, reset, expert graph, dependency, CUDA
toolkit, or driver file was changed.

## Self-review

- Both starter representations instantiate the same
  `S1StarterStateMachine`; adapters only translate typed/script interfaces.
- The typed graph exposes and connects `order-reader`, `task-planner`,
  `waypoint-nav`, and `s1-ablation-driver`.
- Joint, gripper, and base command paths remain interposed by the unchanged
  budget guard. Oracle state is still consumed only by the fixed verifier and
  harness-owned recorder.
- Script semantic gripper commands retain a product label for canonical intent,
  while the protected runtime validates and strips the label before emitting
  the existing scalar guarded command.
- Plan target ordering comes from the existing deterministic task planner.
  Quantities and all targets are retained, but only the first target is acted
  on by design.
- A failed navigation result intentionally advances rather than retrying or
  recovering, protecting the required starter incompleteness.
- The new fixed hub manifest adds `s1-ablation-driver` to
  `registry/schema/curated_core.toml`. That allowlist is Class C and
  CODEOWNERS-protected; the change was TDD-pinned in registry completeness and
  requires human review before merge.

## Fix Round 1

Closed the review finding that the original physical live helper stopped after
the first navigation event. Added a bounded, simulator-free dora dataflow that
feeds controlled neutral goal/order/plan/pose/navigation events through the
actual `s1-ablation-driver` and `script-s1-runtime` node mains, records their
serialized `nav_goal` and `gripper_cmd` outputs, and requires this exact
representation-neutral sequence from both:

```text
nav shelf_zone_A
pick A1-L1-S0#0
nav counter
place A1-L1-S0#0
```

Serialized starter outputs now carry ordered `intent_seq`, `intent_action`,
and `intent_target` metadata. Script commands still pass through whole-batch
validation and scalar gripper serialization; typed commands still use their
node adapter's JSON/scalar wire paths. The controlled test independently
asserts both navigation JSON payloads and both scalar gripper values
`[1.0, 0.0]`.

RED against the one-event implementation:

```text
pytest tests/graph/test_s1_ablation_starters.py::\
test_controlled_dataflow_matches_all_four_serialized_action_intents -q -s

1 failed in 9.87s
KeyError: 'intent_seq'
```

The first GREEN attempt exposed script navigation metadata being overwritten
when its `goal_id` was attached. After preserving the already-built intent
metadata, the focused dataflow test passed:

```text
1 passed in 9.73s
```

After simplifying the metadata adapter, fresh verification passed:

```text
controlled four-action dataflow:
1 passed in 9.67s

shared conformance + protected script regressions:
69 passed in 0.31s

typed topology + controlled dataflow (physical live test deselected):
5 passed, 1 deselected in 9.68s

full unit gate:
639 passed, 1 skipped, 108 deselected in 219.86s

ruff format --check .:
161 files already formatted

ruff check .:
All checks passed

trace check:
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

The physical simulator arms were not rerun. Their seed/goal/oracle evidence
from the original bounded run remains unchanged. The touched oracle digest
canonicalization now explicitly uses `sort_keys=True`. No verifier wait,
dependency/environment change, CUDA workload, or simulator process was
started in this fix round; controlled dora processes were terminated and
reaped from their exact temporary cwd.
