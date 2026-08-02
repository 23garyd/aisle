# Task 6 report — neutral condition adapters

## Scope

Created `src/aisle/harness/ablation_adapters.py` and added the adapter contract
tests to `tests/unit/test_s1_harness_ablation.py`. No frozen environment,
reset, verifier, budget-guard, graph, runtime, specification, or controller
files were changed.

`ConditionAdapter` exposes the controller-facing `preflight`, `rollout`, and
`collect_deliverable` operations. `AisleAdapter` invokes only argv-form
`harness validate` and `harness rollout`, fixed to S1/mobile/teleport/oracle.
`ScriptAdapter` calls `preflight_script` and the fixed protected
`run_script_rollout` runner; it never validates the policy through `harness`.
Both paths record the exact candidate SHA-256 and normalize to
`AttemptResult`.

The AISLE subprocess parser accepts exactly one JSON object and checks that
the process exit status agrees with its `ok` value. It maps process timeouts,
launch errors, and malformed responses to stable `INFRA_*` codes. Validation
is bounded to 30 seconds; rollout receives the existing 420-second Genesis
build allowance plus 2100 seconds per requested S1 seed, matching the
protected script runner.

Script raw trace artifacts remain external audit evidence. Its
`collect_deliverable` returns only the structured `policy_log` path.

## RED evidence

Adapter tests were added before the module existed:

```text
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py -k adapter -q

5 failed: ModuleNotFoundError: aisle.harness.ablation_adapters
```

The timeout-parity regression was then added before changing the rollout
timeout:

```text
1 failed
assert [30.0, 30.0] == [30.0, 2520.0]
```

The guard-evidence fairness regression was also added before its implementation:

```text
1 failed
assert {'timeout': 1} == {'INFRA_SAFETY_UNAVAILABLE': 1}
```

## GREEN evidence

Focused adapter tests after the implementation and fairness hardening:

```text
pytest tests/unit/test_s1_harness_ablation.py -k adapter -q
6 passed, 63 deselected in 0.04s
```

Complete affected unit file:

```text
pytest tests/unit/test_s1_harness_ablation.py -q
69 passed in 0.35s
```

Static and trace gates:

```text
ruff format --check .
162 files already formatted

ruff check .
All checks passed!

python tools/trace_check.py --root .
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

## Integration concerns

1. The existing `harness rollout` JSON does not emit guard counters. It cannot
   be fairly interpreted as `SafetyResult(0, 0, 0)` when the script arm carries
   observed counters. The adapter therefore fails closed with
   `INFRA_SAFETY_UNAVAILABLE`; the new regression test locks this behavior.
   Adding observed AISLE guard counters is an integration change outside Task
   6 and was deliberately not made.
2. `python tools/trace_check.py --strict --root .` remains red for pre-existing
   uncovered `BAL-1`, `FT-2`, `PW-3`, `PW-4`, and `TOOL-1`; normal trace
   coverage is green using the repository's existing waivers.
3. The requested complete `pytest -m unit -q` gate did not finish within the
   command harness: three worktree-pinned attempts reached 56% and kept
   running (the earliest for 2m36s). They had entered the same existing
   simulator/ffmpeg workload already active before this task, contrary to the
   no-sim-run constraint. Only the three exact self-started pytest parent PIDs
   were terminated; the pre-existing workload was left untouched. The focused
   full task file above and all static/normal-trace gates completed.

## Self-review

- AISLE candidate preflight is exactly `harness validate`; AISLE rollout is
  exactly `harness rollout` with shared frozen S1 settings.
- Script rollout has no harness invocation and remains behind the reviewed
  protected wrapper.
- Candidate hashes are computed from exact bytes for both paths.
- Invalid CLI output, timeout, launch failure, invalid seed syntax, missing
  safety evidence, and malformed normalized payloads fail closed.
- The script deliverable boundary excludes raw trace paths.
