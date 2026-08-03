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
- Updated the script adapter, preflight worker, and rollout launcher so every
  trusted script path is rooted in the assigned session worktree.
- Created this report.
- No specification, frozen environment, starter, CUDA, lock, or dependency
  file was changed.

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

The reviewed four-command public interface remains unchanged. Development
attempts use an internal, agent-facing controller command that is omitted from
public help and is available only while the assigned session is running.

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

## Fix round 1 — controller ownership and crash-safe admission

### Review findings closed

1. Development and regression attempts now pass through an authenticated
   Unix-socket service in the already-running external controller. The
   agent-facing command uses the external controller file only as a client;
   it cannot invoke an adapter itself.
   Under an exclusive admission lock, the service validates seeds `0..49`,
   charges requested capacity before the assigned adapter, appends one
   canonical `AttemptResult`, writes a `controller_attempt.json` correlation
   manifest, and settles actual episode spend. The agent receives a
   session-scoped capability, while audit records only its hash and the exact
   external controller hash.
2. Audit now requires a one-to-one admission/result/settlement/controller
   manifest chain, verifies optional adapter-manifest hashes, reports total
   development spend, enforces the requested ceiling, and rejects every other
   run directory as `UNOWNED_LAUNCH`. Settled attempts charge actual episodes;
   crashed/unsettled admissions conservatively charge their full reservation.
   During the agent session, the controller also watches the agent process
   tree and kills canonical dora, Genesis, or harness-rollout launchers that
   are not descendants of the controller-owned broker.
3. `ScriptAdapter` receives the session worktree root. Script preflight,
   worker, wrapper instrumentation, run directory, trusted `PYTHONPATH`, and
   graph budget inputs all resolve from that root; focused tests assert the
   exact paths without launching dora or a simulator.
4. Held-out scoring is serialized on the stable attempts artifact and
   persists a random nonce, nonce-derived run ID, candidate
   hash, start time, and `scoring_started` state before the adapter boundary.
   Concurrent callers reload state only after acquiring the lock. A crash or
   pre-existing same-ID run remains terminal, so a retry cannot execute the
   holdout twice.
5. Completed holdout admission is authorized only by its nonce-bound
   `controller_holdout.json` hash and optional adapter-manifest hash. A
   pre-admission run with the same ID never gains the audit exemption.
6. Every mandatory Claude/Codex usage field must be present as an exact,
   non-negative integer; partial usage objects fail closed.
7. Token and wall ceilings now produce nonzero CLI results and persist the
   terminal `budget_exceeded` state. After stdout EOF, process waiting is
   bounded by only the remaining wall budget before process-group kill.
   Non-finite wall limits are refused before agent launch.
8. Root and subcommand help emit exactly one successful JSON object and keep
   the internal attempt command out of the public command list.

### RED evidence

The first focused review-regression selection was run before the fixes:

```text
UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
PYTHONPATH=$PWD/src:$PWD \
uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k 'supervises_dev_attempts or attempt_channel or unowned_launch or \
explicit_root or scoring_admission or holdout_admission or partial_vendor or \
overshoot_and_wall or stdout_eof or help_is_one' -q

14 failed, 107 deselected in 1.19s
```

The failures reproduced the missing attempt command, unpinned script root,
retryable scoring crash, predictable holdout admission, accepted partial usage
objects, successful budget-stop exits, unbounded executor signature, and
argparse help output.

A later fail-closed scoring-correlation test was also observed RED before its
fix:

```text
1 failed, 121 deselected in 0.26s
```

The wrong attempt ID was initially accepted as a held-out result. Scoring now
validates the canonical result against the nonce-derived run ID and admitted
candidate hash.

### GREEN and final gates

Controller and adapter regression file after formatting:

```text
129 passed
```

Campaign/controller compatibility:

```text
154 passed in 9.66s
```

Repository static gates:

```text
163 files already formatted
All checks passed!
```

Full isolated unit gate before the final broker/race hardening (the affected
combined gate above was rerun afterward):

```text
645 passed, 1 skipped, 168 deselected in 217.03s (0:03:37)
```

Traceability gate:

```json
{"ok":true,"uncovered":[],"errors":[]}
```

`git diff --check` also passed.

### Execution safety

Every added execution boundary was exercised with an authenticated local test
socket, injected fake agent executors, fake condition adapters, fake
subprocesses, or path-only assertions. No real coding agent, dora graph,
Genesis process, simulator episode, development rollout, or held-out rollout
was launched during this fix round.

### Remaining infrastructure boundary

The controller now brokers all authorized adapter launches and kills observed
canonical `dora`, Genesis, and `harness rollout` descendants of the coding
agent. This is stronger evidence than run-directory scanning, but it is not an
OS security boundary. The reviewed campaign launcher deliberately gives
Claude skipped permissions and Codex `danger-full-access`; both run as the
same user with filesystem and network access. An adversarial agent could
rename a launcher or import simulator libraries from an arbitrary Python
script, which process-command inspection cannot prove absent.

Complete prevention therefore requires an ADR-level execution change outside
Task 7: either sandbox the coding agent with a simulator-denying syscall/filesystem
policy while preserving its model connection, or move simulator startup into
a trusted service/binary that rejects every launch without a
controller-private capability. This report does not claim that artifact or
process scanning provides that missing kernel-level confinement.

The confinement blocker now has an approved strong resolution:

- `docs/superpowers/specs/2026-08-02-native-agent-simulator-isolation-design.md`
  (design commit `d78781c`);
- `docs/superpowers/plans/2026-08-02-native-agent-simulator-isolation.md`
  (implementation-plan commit `2fdab3a`).

This fix-round commit is an intermediate checkpoint only. The approved
Bubblewrap/trusted-service design remains to be implemented by that separate
plan before Task 7 can claim complete exclusive simulator brokerage.
