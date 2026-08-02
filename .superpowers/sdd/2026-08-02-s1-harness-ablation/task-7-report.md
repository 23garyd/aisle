# Task 7 report — external campaign controller and budgets

## Status

Implemented `prepare`, `run`, `score`, and `audit` in the external S1
harness-ablation controller. The controller owns deterministic paired
assignment, isolated pinned worktrees, immutable treatment/provenance fields,
live agent-stream token accounting, wall termination, development-episode
capacity, held-out admission, scoring, and contamination audit.

The production boundaries for worktree creation, agent execution, adapters,
clock, frozen audit, and git inspection are dependency-injected through
`ControllerRuntime`. Every test used fixture worktrees, fake agent streams,
and fake adapters.

## Files

- Created `tools/s1_harness_ablation.py`.
- Extended `tests/unit/test_s1_harness_ablation.py`.
- Created this report.
- No specification, frozen environment, runner, starter, adapter, CUDA, lock,
  or dependency file was changed.

Each prepared session has exactly these controller artifacts:

- `session.json`
- `attempts.jsonl`
- `agent.jsonl`
- `token_samples.jsonl`
- `holdout.json`
- `audit.json`

The isolated git worktree is a session directory child, not a controller
artifact. Only the assigned starter is copied to the condition's editable
candidate path.

## RED

Initial controller-focused command:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py -k controller -q
```

Observed before `tools/s1_harness_ablation.py` existed:

```text
FFFFFFFFFFFF
12 failed, 89 deselected in 0.39s
ImportError: cannot import name 's1_harness_ablation' from 'tools'
```

Self-review regressions were also observed RED before their fixes:

- a validly hash-chained early settlement of the controller episode
  reservation returned audit exit `0` instead of `1`;
- a changed run-prompt hash returned audit exit `0` instead of `1`;
- agent-time seed `100` plus a local-baseline manifest returned audit exit `0`
  instead of `1`.

Each was fixed only after its focused failing test reproduced the gap.

## GREEN and gates

Controller suite:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py -k controller -q

18 passed, 89 deselected in 0.65s
```

Campaign/controller compatibility:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync pytest tests/unit/test_campaign.py \
  tests/unit/test_s1_harness_ablation.py -q

132 passed in 6.86s
```

Whole-repository static gates:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
uv run --no-sync ruff format --check .

163 files already formatted

UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
uv run --no-sync ruff check .

All checks passed!
```

Full unit gate:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run --no-sync pytest -m unit -q

645 passed, 1 skipped, 152 deselected in 215.28s (0:03:35)
```

Trace gate:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync python tools/trace_check.py

{"ok": true, ..., "uncovered": [], "errors": []}
```

Actual CLI error-path smoke:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync python tools/s1_harness_ablation.py

{"code":"ARGUMENT","error":"the following arguments are required: command","ok":false}
```

The smoke exited `1` and emitted exactly one JSON object.

## Implemented integrity behavior

- Pair-adjacent `aisle`/`script` assignment is deterministic from the
  assignment seed, and audit recomputes it.
- Pin, prepared prompt, actual run prompt, controller, starter, initial
  candidate, and all seed-domain hashes are recorded and audited.
- The research process receives the session worktree at the front of
  `PYTHONPATH`.
- Live Claude/Codex events are parsed strictly before `UsageCounter` sees
  them. Malformed, missing, negative, concatenated, or contradictory usage
  fails closed; missing telemetry never becomes zero spend.
- A global nonblocking simulator-authority lock prevents two controller
  sessions from running concurrently.
- The frozen harness campaign ledger is pre-reserved so only the requested
  (at most 40) development episodes remain. Scoring releases that reservation
  only after the agent stops. Audit validates both the cryptographic chain and
  the reservation/settlement semantics.
- Held-out scoring accepts only `100..107`, only after agent stop, exactly
  once. Missing deliverables score explicit pass@1 `0.0`.
- Partial held-out execution and `INFRA_SAFETY_UNAVAILABLE` fail closed with
  pass@1 `null`; safety unavailability is never inferred as observed zero.
- Audit covers assignment, pin/frozen/dirty drift, provenance, token
  telemetry, operator events, overlapping session intervals, both ledgers,
  artifact hashes, malformed attempts, missing safety evidence, untrusted
  local-baseline rollouts, and agent-time held-out seed exposure.

## Self-review

The reviewed diff is limited to the requested controller, shared ablation unit
file, and this report. `git diff --check` is clean.

The main design compromise follows the reviewed four-command interface:
`attempts.jsonl` is initialized, integrity-checked, and consumed as canonical
`AttemptResult` rows, but the interface does not add an unreviewed
development-attempt RPC. Existing in-session harness rollouts remain evidenced
by their protected run manifests, episode files, and budget ledger; audit
checks those artifacts for held-out and local-baseline contamination. A future
operator-facing attempt RPC should be designed explicitly rather than silently
expanding this task's frozen CLI.

## Proof no research or simulator session ran

- All controller tests replaced agent execution with `ExecutionResult`
  fixtures and supplied in-memory Claude JSON lines.
- All scoring tests used fake adapters or deleted fixture candidates before
  scoring.
- Worktree tests copied three local fixture files; they did not call
  `git worktree`, `uv sync`, Claude, Codex, dora, harness rollout, Genesis, or
  either real condition adapter.
- Commands run for this task were pytest unit selection, ruff, trace check,
  read-only git/process inspection, and the controller's missing-argument
  error path.
- No paid token session, research session, pilot/main campaign, held-out
  simulation, graph/sim marker, or simulator workload was launched.
