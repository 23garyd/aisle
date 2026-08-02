# Task 3 report — protected script policy contract and preflight

## Scope

Implemented the agent-editable script baseline contract and its fixed, isolated
preflight boundary. The implementation does not import Genesis, start dora, or
launch any simulator/runtime process.

Files changed:

- `baselines/script_s1/__init__.py`
- `baselines/script_s1/contract.py`
- `baselines/script_s1/starter.py`
- `src/aisle/harness/script_preflight.py`
- `src/aisle/harness/script_preflight_worker.py`
- `tests/unit/test_s1_harness_ablation.py`

## Design

- `PolicyEvent` and `PolicyCommand` are frozen dataclasses. They reject
  non-JSON-compatible payloads, non-finite numeric values, invalid command
  kinds, and invalid simulation times.
- The starter records ordered goal items and produces one navigation, close,
  and delivery sequence. It intentionally has no grasp confirmation, retry,
  recovery, or quantity-management loop.
- `preflight_script` uses the current interpreter with `-I`, a ten-second
  timeout, and a fixed bootstrap to load the worker from this worktree. The
  bootstrap is necessary because the mandated no-sync environment's editable
  `aisle` installation points to the main checkout rather than this worktree.
  It adds only the fixed worktree package path and never adds the candidate
  directory or exposes it through `PYTHONPATH`.
- The worker compiles/imports the candidate, checks `create_policy`, checks
  `on_event`, sends one synthetic `episode_goal`, and emits exactly one JSON
  object. Parent-side results are normalized to the required stable codes:
  `SCRIPT_SYNTAX`, `SCRIPT_IMPORT`, `FACTORY_MISSING`, `POLICY_INVALID`, and
  `COMMAND_INVALID`.

## RED evidence

Added temporary-candidate tests for syntax error, import error, missing
factory, missing handler, invalid command kind, and a valid policy. Before the
implementation, the required focused command failed with six
`ModuleNotFoundError: aisle.harness.script_preflight` failures:

```text
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k script_preflight -q
# 6 failed, 15 deselected
```

An isolated-starter regression was then added after a manual boundary check
found its relative import could not resolve as an isolated candidate. Its RED
run was `1 failed, 6 passed, 15 deselected`, with `SCRIPT_IMPORT`; switching to
the stable public contract import made the starter pass.

## GREEN and verification

```text
focused preflight tests: 7 passed, 15 deselected in 0.52s
ruff format --check .: 151 files already formatted
ruff check .: All checks passed
python tools/trace_check.py: {"ok": true, ..., "uncovered": []}
git diff --check: passed
```

The full unit gate was run after the implementation with JUnit capture:

```text
pytest -m unit -q --junitxml=/tmp/aisle-task3-unit.xml
630 tests, 1 failure, 1 skipped (215.013s)
```

The sole failure is `tests/unit/test_validator.py::test_expert_t0_is_good`.
It is outside this task's paths and arises because that test's CLI subprocess
uses the shared no-sync venv's editable `aisle` mapping at
`/home/demo/Public/github_aisle/aisle-latest/src`, rather than this worktree;
it reports an existing expert graph manifest/source mismatch. The task's
focused suite is green and the preflight launcher explicitly handles this
worktree-resolution condition.

## Self-review

- Verified the candidate is never imported by the parent process.
- Verified the worker contains no Genesis or dora imports and no runtime
  launch logic.
- Verified all invalid paths map to only the required stable codes.
- Verified the parent enforces the ten-second timeout.
- Kept changes limited to the task-3 files and focused tests; no frozen files,
  specs, environment files, simulators, dora processes, or experiment sessions
  were changed or started.
