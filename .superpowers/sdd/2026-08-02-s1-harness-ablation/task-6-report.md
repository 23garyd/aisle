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

## Fix Round 1 — usable public safety records and adapter parity

### RED evidence

New focused tests were added before the integration change. They produced the
following expected failures:

```text
test_script_adapter_maps_invalid_rollout_arguments_without_launching_runner
2 failed: {'INFRA_PROTOCOL': 1} != {'INFRA_ARGUMENT': 1}

test_script_adapter_maps_missing_candidate_to_stable_argument_failure
FileNotFoundError from sha256_file(missing.py)

test_script_adapter_preflight_failure_measures_elapsed_wall_time
TypeError: ScriptAdapter.__init__() got an unexpected keyword argument 'clock'

test_rollout_exports_observed_guard_stats_and_extra_item_safety
KeyError: 'safety'
```

The initial rollout test invocation used the shared editable parent checkout
and therefore continued to report the old missing key after implementation.
The worktree-pinned rerun below is the authoritative result.

### GREEN evidence

`rollout.observed_safety` reads the final cumulative `violations` object from
each recorded `budget-guard__guard_stats.arrow` stream, sums its observed
counts across relaunches, and derives `extra_item` from episode outcomes. A
guard-stats trace is mandatory: absent, malformed, or empty evidence yields
`"safety": null`; `AisleAdapter` maps that to
`INFRA_SAFETY_UNAVAILABLE`. Thus no zero clamp or ungated count is reported
without the validated topology plus an observed guard counter stream.

The script adapter now validates seed syntax and run IDs before its protected
runner, maps runner `ValueError` and missing policy input to `INFRA_ARGUMENT`,
and uses an injected monotonic clock to measure timeout/launch-preflight
wall time. A missing file has no byte hash; its refusal record uses a
deterministic domain-separated missing-candidate identity hash solely to meet
the immutable result schema while clearly carrying `INFRA_ARGUMENT`.

```text
PYTHONPATH=<worktree>/src:<worktree> uv run --no-sync pytest \
  tests/unit/test_s1_harness_ablation.py \
  tests/unit/test_rollout_metrics.py -q
84 passed in 0.50s

ruff format --check src/aisle/harness/rollout.py \
  src/aisle/harness/ablation_adapters.py \
  tests/unit/test_rollout_metrics.py tests/unit/test_s1_harness_ablation.py
4 files already formatted

ruff check <same four files>
All checks passed!

ruff format --check . && ruff check .
162 files already formatted
All checks passed!

python tools/trace_check.py --root .
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

The full `pytest -m unit -q` gate was not rerun: the same pre-existing ffmpeg
simulator workload recorded above remains active, and the task prohibits new
simulation runs. No simulator, pilot, or research workload was started for
this fix round.

## Fix Round 2: complete launch evidence and absent candidate identity

### RED evidence

Before implementation, the new focused tests failed as intended:

```text
tests/unit/test_rollout_metrics.py -k 'observed_safety or rollout_exports'
4 failed
TypeError: observed_safety() got an unexpected keyword argument 'topology_validated'
AssertionError: report["safety"] was None

tests/unit/test_s1_harness_ablation.py -k 'reserves_zero_hash or maps_missing_candidate'
2 failed
```

### GREEN evidence

`rollout.observed_safety` now receives the explicit list of initial-launch
and relaunch trace directories. It requires every expected directory to
contain a non-empty, readable `budget-guard__violation.arrow` stream with a
`text` column and valid violation JSON. It counts every row once, including
the final shutdown-tail row, and returns unavailable evidence for a missing,
empty, malformed, or textless stream. `ungated: 0` is emitted only when that
complete evidence is present and topology validation was supplied.

The all-zero SHA-256 value is reserved in `AttemptResult.from_dict` for an
absent candidate only: it requires `failures == {"INFRA_ARGUMENT": 1}` and
`artifacts["candidate_identity"] == "absent"` (additional artifact fields
remain permitted). Conversely, a nonzero byte hash cannot carry that absent
identity. Both adapters produce that form only for a missing candidate;
ordinary real files continue to use their SHA-256 byte hash.

```text
PYTHONPATH=<worktree>/src:<worktree> uv run --no-sync pytest \
  tests/unit/test_s1_harness_ablation.py \
  tests/unit/test_rollout_metrics.py -q
88 passed in 0.57s

ruff format --check .
162 files already formatted

ruff check .
All checks passed!

python tools/trace_check.py --root .
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

No simulation, pilot, or research workload was run. The pre-existing ffmpeg
simulator process remains active, so the complete `pytest -m unit -q` gate
was intentionally not started for this fix round.

## Fix Round 3: complete Arrow streams and candidate identity construction

### RED evidence

The new EOS regression showed that PyArrow accepts rows from a stream after
the final eight-byte Arrow IPC EOS marker has been removed:

```text
test_observed_safety_rejects_violation_stream_without_eos_marker
AssertionError: observed_safety(...) == {"ungated": 0, "clamps": 1, ...}
```

The focused script and identity tests also exposed the pre-fix behavior:

```text
9 failed, 1 passed
* direct AttemptResult(...) accepted zero/nonzero candidate identity conflicts
* missing and malformed script guard streams reported ordinary timeout/zero safety
* a textless script trace raised KeyError
* directory candidates were labeled absent
```

A final parser regression demonstrated that Arrow can stop at an earlier EOS
and ignore bytes appended before a second EOS; the parser now verifies that
the Arrow reader consumed the complete payload.

### GREEN evidence

`complete_violation_stream` is the single parser used by public AISLE rollout
and protected script rollout. It requires the terminal Arrow IPC EOS marker,
validates every `text` row as a direct violation JSON record, and verifies
that the reader consumed all supplied bytes. Missing, empty, malformed,
textless, EOS-truncated, or trailing-byte streams become unavailable safety
evidence. Script rollout records `INFRA_SAFETY_UNAVAILABLE` rather than
silently treating that state as zero clamps; both adapters expose the same
stable failure shape.

`AttemptResult.__post_init__` now owns the candidate sentinel invariant, so
direct construction and deserialization apply the same rules. The reserved
zero hash requires exactly `{"INFRA_ARGUMENT": 1}` and an explicit
`candidate_identity` of `absent` or `invalid`; no nonzero byte hash can carry
either identity. Only `FileNotFoundError` receives `absent`; directories,
permission errors, other OS errors, and a forced zero digest are `invalid`.

```text
PYTHONPATH=<worktree>/src:<worktree> uv run --no-sync pytest \
  tests/unit/test_s1_harness_ablation.py \
  tests/unit/test_s1_ablation_conformance.py \
  tests/unit/test_rollout_metrics.py -q
106 passed in 0.54s

ruff format --check .
162 files already formatted

ruff check .
All checks passed!

python tools/trace_check.py --root .
{"ok": true, "uncovered": [], "unknown_citations": [], "errors": []}
```

No simulation, pilot, or research workload was started. The complete
`pytest -m unit -q` gate remains intentionally unrun because the pre-existing
ffmpeg simulator workload is active.

## Fix Round 4: record parity and durable candidate identity

### RED evidence

The new mutation, resolver, and real-runner parity regressions were added
before production changes:

```text
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH=<worktree>/src:<worktree> \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k 'serialization_rejects_mutated or resolution_errors or preserve_equivalent_records' -q

6 failed, 84 deselected in 0.39s
* both mutation probes serialized invalid candidate identities
* PermissionError, OSError, and resolver-stage FileNotFoundError escaped
* AISLE discarded episodes, the existing timeout, sim timing, and artifacts
```

### GREEN evidence

`normalize_safety_evidence` is now shared by the public adapter normalizer and
the protected script runner. Unavailable evidence adds the stable
`INFRA_SAFETY_UNAVAILABLE` count while retaining the rest of the rollout
record. The parity regression uses `ScriptAdapter`'s real default
`run_script_rollout` path with only the external dora launch boundary replaced;
it compares every `AttemptResult` field except the explicitly condition-specific
candidate hash and artifacts.

`AttemptResult.to_dict()` revalidates the candidate sentinel invariant against
the current mutable mappings. Candidate resolution and byte hashing are now one
ordered adapter boundary: every resolver `OSError` is `invalid`, while only a
`FileNotFoundError` opening the resolved file is `absent`.

```text
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH=<worktree>/src:<worktree> \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k 'serialization_rejects_mutated or resolution_errors or preserve_equivalent_records' -q

6 passed, 84 deselected in 0.16s

env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH=<worktree>/src:<worktree> \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  tests/unit/test_s1_ablation_conformance.py tests/unit/test_rollout_metrics.py -q

111 passed in 0.60s

env -u PYTHONPATH UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  uv run --no-sync ruff format --check .
162 files already formatted

env -u PYTHONPATH UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  uv run --no-sync ruff check .
All checks passed!

env -u PYTHONPATH UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  uv run --no-sync python tools/trace_check.py --root .
exit 0; ok=true, uncovered=[], unknown_citations=[], errors=[]

git diff --check
exit 0
```

### Self-review

- The prior handcrafted unavailable-safety parity fake was removed; the
  regression reaches the protected default runner and retains episodes,
  failures, safety, timing, and preflight exactly across arms.
- Resolver-stage `FileNotFoundError` is deliberately `invalid`; the existing
  real missing-file test still proves the distinct `absent` identity.
- The complete Arrow parser and missing/textless/malformed/truncated/trailing
  stream behavior were not modified; the rollout metrics coverage stayed green.
- Independent read-only review found no Critical, Important, or Minor issues
  and returned PASS.

No simulation, CUDA, sync, reinstall, pilot, or research workload was started.
Per the task constraint, verification was limited to the focused
schema/adapter/rollout tests rather than the broad unit gate.
