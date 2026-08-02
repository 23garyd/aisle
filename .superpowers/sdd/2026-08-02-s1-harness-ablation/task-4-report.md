# Task 4 report: fixed script runtime and external safety topology

## Status

Implemented the protected S1 script execution boundary, fixed wrapper graph,
manifest, neutral rollout helper, focused topology/runtime/rollout tests, and a
live CUDA safety smoke. No frozen scene, verifier, reset, or expert graph file
was modified.

Requirement traceability used by the tests: `CON-5`, `CON-7`, `CON-8`,
`BG-1..3`, `MOB-3`, and `HAR-1`.

## RED evidence

1. Topology tests:

   ```text
   tests/unit/test_s1_harness_ablation.py -k script_wrapper -q
   5 failed, 24 deselected
   FileNotFoundError: graphs/ablation_script_s1_wrapper.yaml
   ```

2. Runtime tests:

   ```text
   tests/unit/test_s1_harness_ablation.py -k script_runtime -q
   4 failed, 29 deselected
   ModuleNotFoundError: aisle.nodes.script_s1_runtime
   ```

3. Rollout helper tests:

   ```text
   tests/unit/test_s1_harness_ablation.py -k run_script_rollout -q
   2 failed, 33 deselected
   ModuleNotFoundError: aisle.harness.script_rollout
   ```

4. Cleanup regression after the first live diagnostic left only the two
   candidate-hosting runtime subprocess layers:

   ```text
   tests/unit/test_s1_harness_ablation.py -k orphan_reaper -q
   1 failed, 35 deselected
   nodes/script_s1_runtime.py not in NODE_PATTERNS
   ```

5. Registry integration:

   ```text
   test_all_lint: 28 checked vs 29 files
   test_registry_completeness: fixed hub runtime absent from curated core
   ```

## GREEN evidence

- Required focused unit suite:

  ```text
  env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
    uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py -q
  36 passed in 0.27s
  ```

- Curated registry integration:

  ```text
  pytest test_all_lint test_registry_completeness
  2 passed in 0.25s
  python -m aisle.harness.registry lint --root .
  {"ok": true, "checked": 29, "errors": [], "warnings": []}
  ```

- Fixed graph validation, with explicit worktree root:

  ```text
  harness validate graphs/ablation_script_s1_wrapper.yaml \
    --root . --embodiment mobile
  {"ok": true, "errors": [], "warnings": []}
  ```

- Formatting and lint:

  ```text
  uv run --no-sync ruff format --check .
  154 files already formatted
  uv run --no-sync ruff check .
  All checks passed
  ```

- Full unit gate attempt with trusted worktree `PYTHONPATH`:

  ```text
  417 passed, 1 skipped, 79 deselected in 173.02s
  ```

  This gate was interrupted in the unrelated, CPU-heavy
  `test_pick_solves_across_the_nav_tolerance_envelope`; it had no failures at
  interruption and is recorded as incomplete, not passed.

## Live simulator / GPU evidence

- Existing environment only; no sync, reinstall, CUDA toolkit, or driver
  change.
- GPU: NVIDIA GeForce RTX 5090, driver `580.126.09`.
- Automatic configured backend was used. The live bridge consumed about
  1793 MiB GPU memory.
- Exactly one simulator workload was run at a time.
- Clean smoke:

  ```text
  tests/graph/test_s1_script_wrapper.py -q -s
  1 passed in 320.64s
  ```

  The starter emitted `gripper_cmd == [1.0]`; the fixed budget guard emitted a
  different safe value and a `position`/`velocity` violation. The executable
  graph wired Genesis only to `budget-guard/gripper_cmd_safe`, proving the
  unsafe request could not arrive unmodified.

  The starter's current `{"target": ...}` navigation intent is intentionally
  incomplete, while the frozen retail verifier uses
  `verifier/placement.toml`'s 600-sim-second deadline and ignores the shorter
  rollout goal timeout. The smoke therefore exercised the brief's permitted
  documented task-failure branch instead of waiting hours for an episode
  result. No task-owned dora, Genesis, or script-runtime process remained
  after explicit cleanup.

## Files

- `graphs/ablation_script_s1_wrapper.yaml`
- `registry/manifests/script-s1-runtime.yaml`
- `registry/schema/curated_core.toml`
- `src/aisle/nodes/script_s1_runtime.py`
- `src/aisle/harness/script_rollout.py`
- `src/aisle/harness/reaper.py`
- `tests/unit/test_s1_harness_ablation.py`
- `tests/unit/test_manifests.py`
- `tests/graph/test_s1_script_wrapper.py`

## Self-review

- Candidate policy receives no `oracle_state`.
- All joint, gripper, and navigation-derived base motion reaches Genesis only
  through the unchanged `budget-guard`.
- Reset/verifier and all common fixed node source paths match
  `graphs/expert_s1.yaml`.
- Returned command batches are fully validated before the first emission.
  Invalid batches terminate with `COMMAND_INVALID`; well-shaped unsafe numeric
  values are preserved for the guard rather than silently coerced.
- `run_script_rollout` launches only the repository-owned wrapper, never calls
  candidate graph validation/preflight, captures raw traces for external audit,
  exposes only structured policy-log artifacts, parses neutral results, and
  reaps the candidate-hosting runtime.
- Shared editable environment limitation: without explicit `--root .` or
  worktree-local trusted `PYTHONPATH`, subprocesses resolve the parent checkout.
  Candidate directories are never added to `PYTHONPATH`.
- `registry/schema/curated_core.toml` is Class C / CODEOWNERS-protected. Its
  minimal addition is required because the protected runtime is fixed hub
  infrastructure rather than an agent-authored evalcarded skill; human review
  is required before merge.
