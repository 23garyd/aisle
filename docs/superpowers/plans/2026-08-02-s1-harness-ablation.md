# S1 Harness-versus-Script Ablation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fair, externally controlled experiment that compares
agentic S1 improvement through AISLE's typed harness against improvement of an
interface-matched monolithic Python policy.

**Architecture:** A neutral controller owns randomization, budgets,
provenance, scoring, and audits. Two adapters expose different engineering
surfaces while sharing the same Genesis scene, observations, robot primitives,
external safety guard, verifier, reset, seeds, and scoring schema.

**Tech Stack:** Python 3.13, uv, pytest, dora 1.0.0-rc.4, Genesis 1.2.3,
PyArrow, JSON/JSONL, existing AISLE campaign and rollout infrastructure.

## Global Constraints

- Do not run pilot or main-study agent sessions during implementation.
- The script baseline must not be intentionally blinded or made unsafe.
- Verifier, reset, environment, and budget guard remain frozen and identical.
- Held-out seeds `100..107` are visible only to the external scorer.
- Development seeds are `0..49`; regression seeds are `0..7`.
- Initial per-session ceilings are 500,000 new tokens, 40 development
  episodes, and four wall-clock hours.
- Only one simulator workload may run at a time.
- Every CLI emits one JSON object to stdout and exits zero iff `"ok": true`.

---

### Task 1: Protocol ADR and neutral attempt schema

**Files:**
- Create: `docs/decisions/ADR-s1-harness-ablation.md`
- Create: `src/aisle/harness/ablation.py`
- Create: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Produces: `PreflightResult`, `AttemptResult`, `SafetyResult`
- Produces: `AttemptResult.from_dict(value: dict) -> AttemptResult`
- Produces: `AttemptResult.to_dict() -> dict`
- Consumed by: both condition adapters and the campaign controller

- [ ] **Step 1: Write the protocol ADR**

Record the approved design from
`docs/superpowers/specs/2026-08-02-s1-harness-ablation-design.md`, including
conditions, budgets, seeds, exclusions, pilot/main split, and the rule that
no-deliverable sessions score zero.

- [ ] **Step 2: Add failing schema tests**

Add:

```python
def test_attempt_result_round_trip_and_rejects_unknown_fields():
    """HAR-1, CON-5: both ablation arms emit one canonical attempt record."""
    from aisle.harness.ablation import AttemptResult

    raw = {
        "attempt_id": "A-0001",
        "candidate_hash": "a" * 64,
        "preflight": {"ok": True, "errors": [], "wall_s": 0.25},
        "episodes": [],
        "failures": {},
        "safety": {"ungated": 0, "clamps": 0, "extra_item": 0},
        "timing": {"wall_s": 1.0, "sim_s": 0.0},
        "artifacts": {},
    }
    assert AttemptResult.from_dict(raw).to_dict() == raw
    with pytest.raises(ValueError, match="unknown"):
        AttemptResult.from_dict({**raw, "condition": "aisle"})
```

Also test a malformed SHA, negative time, non-list episodes, and negative
safety counts.

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py -q
```

Expected: import failure because `aisle.harness.ablation` does not exist.

- [ ] **Step 4: Implement strict immutable schema types**

Implement frozen dataclasses. Parse only exact keys and validate all numeric
fields as finite and non-negative:

```python
@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[dict, ...]
    wall_s: float


@dataclass(frozen=True)
class SafetyResult:
    ungated: int
    clamps: int
    extra_item: int


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    candidate_hash: str
    preflight: PreflightResult
    episodes: tuple[dict, ...]
    failures: dict[str, int]
    safety: SafetyResult
    timing: dict[str, float]
    artifacts: dict[str, str]
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 3 command. Expected: all schema tests pass.

---

### Task 2: Tamper-evident session ledger and assignment

**Files:**
- Modify: `src/aisle/harness/ablation.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Produces: `paired_assignments(seed: int, pairs: int) -> list[str]`
- Produces: `append_ledger(path: Path, event: dict) -> str`
- Produces: `verify_ledger(path: Path) -> tuple[bool, str | None]`
- Produces: `sha256_file(path: Path) -> str`

- [ ] **Step 1: Add failing assignment and ledger tests**

Test that every pair contains exactly `{"aisle", "script"}`, the same seed
reproduces the same order, and different seeds change at least one pair.

Test a three-event JSONL ledger:

```python
head = append_ledger(path, {"kind": "session_start", "session": "P01-A"})
head = append_ledger(path, {"kind": "attempt", "attempt": "A-0001"})
head = append_ledger(path, {"kind": "session_end", "status": "agent_done"})
assert verify_ledger(path) == (True, head)
```

Mutating the middle line must make verification fail.

- [ ] **Step 2: Verify RED**

Run the focused unit file. Expected: missing functions.

- [ ] **Step 3: Implement deterministic pairing and hash chaining**

Each JSONL event must contain:

```json
{
  "seq": 1,
  "prev_sha256": "64 hex chars or null",
  "event": {},
  "sha256": "sha256(canonical JSON of seq, prev_sha256, event)"
}
```

Canonical JSON uses `sort_keys=True` and compact separators. Append under an
exclusive file lock and `fsync` before returning.

- [ ] **Step 4: Verify GREEN**

Run the focused unit file. Expected: assignment and tamper tests pass.

---

### Task 3: Protected script policy contract and preflight

**Files:**
- Create: `baselines/script_s1/__init__.py`
- Create: `baselines/script_s1/contract.py`
- Create: `baselines/script_s1/starter.py`
- Create: `src/aisle/harness/script_preflight.py`
- Create: `src/aisle/harness/script_preflight_worker.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Produces: `PolicyEvent(kind: str, payload: dict, sim_time_ns: int)`
- Produces: `PolicyCommand(kind: Literal["nav_goal", "joint_cmd", "gripper_cmd"], payload: list[float] | dict)`
- Requires policy module interface:
  `create_policy(seed: int) -> object` with
  `on_event(event: PolicyEvent) -> list[PolicyCommand]`
- Produces: `preflight_script(path: Path) -> PreflightResult`

- [ ] **Step 1: Add failing contract tests**

Create temporary scripts covering:

- syntax error;
- import error;
- missing `create_policy`;
- policy without `on_event`;
- invalid command kind;
- valid policy.

Assert stable error codes:

```text
SCRIPT_SYNTAX
SCRIPT_IMPORT
FACTORY_MISSING
POLICY_INVALID
COMMAND_INVALID
```

- [ ] **Step 2: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --no-sync pytest \
  tests/unit/test_s1_harness_ablation.py -k script_preflight -q
```

Expected: missing module/functions.

- [ ] **Step 3: Implement the pure policy contract**

Use frozen dataclasses with JSON-compatible payload validation. The starter
must be intentionally incomplete: it tracks ordered items and emits one
navigation/pick/deliver sequence without explicit grasp confirmation, retry,
or dropped-item recovery.

- [ ] **Step 4: Implement isolated preflight**

Run import and a synthetic `episode_goal` event in a subprocess using the
current interpreter:

```text
python -I -m aisle.harness.script_preflight_worker <absolute-policy-path>
```

The worker returns one JSON object. It must not import Genesis or start dora.
The parent imposes a 10-second timeout and maps every failure to a stable code.

- [ ] **Step 5: Verify GREEN**

Run the Step 2 command. Expected: all contract/preflight cases pass.

---

### Task 4: Fixed script runtime and external safety topology

**Files:**
- Create: `src/aisle/nodes/script_s1_runtime.py`
- Create: `src/aisle/harness/script_rollout.py`
- Create: `graphs/ablation_script_s1_wrapper.yaml`
- Create: `registry/manifests/script-s1-runtime.yaml`
- Modify: `tests/unit/test_s1_harness_ablation.py`
- Create: `tests/graph/test_s1_script_wrapper.py`

**Interfaces:**
- Consumes env: `AISLE_SCRIPT_POLICY` absolute path and `AISLE_SEED`
- Consumes dora topics: `episode_goal`, `poses`, `joint_state`, `base_pose`,
  `nav_result`, `reset_done`
- Produces: `nav_goal`, `joint_cmd`, `gripper_cmd`, `policy_event`
- Produces:
  `run_script_rollout(policy: Path, seeds: str, run_id: str) -> AttemptResult`
- Motion outputs route through the unchanged `budget-guard`

- [ ] **Step 1: Add failing topology tests**

Parse the wrapper YAML and assert:

- agent-editable policy never receives `oracle_state`;
- every arm/base command reaches Genesis only through `budget-guard`;
- reset and verifier use the same source files as `expert_s1.yaml`;
- the script runtime is the only editable behavioral node;
- the agent-facing log is `policy_event`, not raw per-node traces.

- [ ] **Step 2: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --no-sync pytest \
  tests/unit/test_s1_harness_ablation.py -k script_wrapper -q
```

Expected: wrapper file missing.

- [ ] **Step 3: Implement the runtime**

The fixed runtime translates dora events to `PolicyEvent`, calls
`policy.on_event`, validates returned commands, and emits only declared
commands. Invalid commands terminate the node with `COMMAND_INVALID`; they
must never be coerced.

The wrapper uses the same Genesis store scene, rollout client, reset,
verifier-retail, nav-action, and budget-guard nodes as S1.

`run_script_rollout` owns graph launch, timeout, structured result parsing,
and cleanup for this fixed wrapper. It never runs candidate graph validation;
candidate-specific rejection belongs to `preflight_script`.

- [ ] **Step 4: Verify topology GREEN**

Run the Step 2 command. Expected: all topology assertions pass.

- [ ] **Step 5: Add a graph smoke test**

Run the starter for one development seed long enough to produce a structured
episode result or documented task failure. Assert that a guard violation
attempt is clamped and cannot reach Genesis unmodified.

---

### Task 5: Equivalent AISLE starter and conformance

**Files:**
- Create: `src/aisle/harness/s1_ablation_common.py`
- Create: `src/aisle/nodes/s1_ablation_driver.py`
- Create: `graphs/ablation_s1_starter.yaml`
- Create: `registry/manifests/s1-ablation-driver.yaml`
- Create: `tests/unit/test_s1_ablation_conformance.py`
- Create: `tests/graph/test_s1_ablation_starters.py`

**Interfaces:**
- AISLE starter exposes typed `order-reader`, `task-planner`,
  `waypoint-nav`, and `s1-ablation-driver` nodes.
- Script starter uses the same pure starter state machine through its
  monolithic policy contract.
- Produces: `canonical_action_intent(events: list[dict]) -> list[dict]`

- [ ] **Step 1: Add failing conformance tests**

For a fixed synthetic S1 goal and pose sequence, assert both starters produce
the same normalized intent sequence:

```text
nav shelf_zone
pick product_id
nav counter
place product_id
```

Also assert identical initial quantities, target ordering, and seed handling.

- [ ] **Step 2: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --no-sync pytest tests/unit/test_s1_ablation_conformance.py -q
```

- [ ] **Step 3: Implement the shared incomplete behavior**

Move the minimal deterministic state-transition logic into
`src/aisle/harness/s1_ablation_common.py`. Both representations consume it at
the starter revision, but campaign prompts permit agents to replace their
assigned representation.

Do not include grasp confirmation, retry, recovery, or robust quantity
tracking.

- [ ] **Step 4: Verify unit and live conformance**

Run unit conformance, validate the AISLE graph, then run both starters on the
same development seed. Compare goal hash, initial oracle-state digest,
normalized intent, guard configuration, and verifier result schema.

---

### Task 6: Neutral condition adapters

**Files:**
- Create: `src/aisle/harness/ablation_adapters.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Defines protocol:

```python
class ConditionAdapter(Protocol):
    def preflight(self, candidate: Path) -> PreflightResult: ...
    def rollout(self, candidate: Path, seeds: str, run_id: str) -> AttemptResult: ...
    def collect_deliverable(self, candidate: Path) -> dict[str, str]: ...
```

- Produces: `AisleAdapter` and `ScriptAdapter`

- [ ] **Step 1: Add failing adapter contract tests**

Use subprocess fakes to assert exact command construction:

- AISLE preflight calls `harness validate`.
- AISLE rollout calls `harness rollout`.
- Script preflight calls `preflight_script`.
- Script rollout calls the protected script wrapper runner without invoking
  `harness validate` on the candidate.
- Both normalize failures, safety, timing, and artifacts identically.

- [ ] **Step 2: Verify RED**

Run the adapter-focused unit selection. Expected: missing adapters.

- [ ] **Step 3: Implement adapters**

Use argv lists, never shell strings. Enforce one JSON object on stdout,
timeouts, stable infrastructure error codes, and candidate SHA-256 recording.

The script runner may retain full raw traces for external audit, but
`collect_deliverable` exposes only the structured policy log to the agent.

- [ ] **Step 4: Verify GREEN**

Run all adapter tests and confirm equivalent neutral records for equivalent
fake results.

---

### Task 7: External campaign controller and budgets

**Files:**
- Create: `tools/s1_harness_ablation.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- CLI:

```text
python tools/s1_harness_ablation.py prepare
  --pin <oid> --pairs <n> --assignment-seed <int> --out <path>

python tools/s1_harness_ablation.py run
  --session <id> --condition aisle|script --agent claude|codex
  --model <name> --tokens 500000 --episodes 40 --wall-h 4

python tools/s1_harness_ablation.py score
  --session <id> --holdout 100..107

python tools/s1_harness_ablation.py audit --dir <campaign-dir>
```

- Reuses: token parsers and agent session machinery from `tools/campaign.py`
- Produces: `session.json`, `attempts.jsonl`, `agent.jsonl`,
  `token_samples.jsonl`, `holdout.json`, and `audit.json`

- [ ] **Step 1: Add failing CLI tests**

Test:

- deterministic paired preparation;
- disjoint seed enforcement;
- token/wall/episode refusal;
- condition cannot change after session start;
- held-out scoring refused before agent stop;
- no-deliverable score is zero;
- malformed agent telemetry fails closed;
- stdout contains exactly one JSON object.

- [ ] **Step 2: Verify RED**

Run the controller-focused tests. Expected: tool missing.

- [ ] **Step 3: Implement `prepare`**

Resolve the pin, create isolated session worktrees, copy only the assigned
starter surface, hash runner/prompt/starter/seeds, and write randomized
assignments without treatment outcomes.

- [ ] **Step 4: Implement `run`**

Reuse `UsageCounter`, live stream capture, wall termination, and worktree
auditing from existing campaign machinery. The external controller is the sole
budget authority.

- [ ] **Step 5: Implement `score` and `audit`**

Score the final deliverable through its adapter on held-out seeds, then audit
frozen drift, prompt/runner/starter hashes, telemetry, operator events,
concurrent simulator evidence, and ledger integrity.

- [ ] **Step 6: Verify GREEN**

Run the controller unit suite. All modes must satisfy CON-8.

---

### Task 8: Regression and metric derivation

**Files:**
- Create: `tools/s1_harness_ablation_analysis.py`
- Create: `tests/unit/test_s1_harness_ablation_analysis.py`

**Interfaces:**
- Produces:
  `analyze_session(session_dir: Path) -> dict`
- Produces:
  `analyze_campaign(campaign_dir: Path) -> dict`
- Output fields:
  `heldout_pass1`, `working`, `first_success_wall_s`,
  `first_success_tokens`, `first_success_episodes`, `invalid_attempts`,
  `preflight_savings`, `regressions`, `regression_recoveries`,
  `safety`, `exclusions`

- [ ] **Step 1: Add fixture-driven failing tests**

Cover:

- successful session;
- no deliverable;
- invalid preflight caught before rollout;
- startup-invalid script;
- one lost regression seed and later recovery;
- ungated safety invalidation;
- excluded contaminated session;
- missing telemetry fail-closed;
- aggregation by condition without dropping zero outcomes.

- [ ] **Step 2: Verify RED**

Run the analysis test file. Expected: tool missing.

- [ ] **Step 3: Implement machine-derived metrics**

Derive metrics only from ledgers, attempts, token samples, and held-out files.
Never accept headline metric fields written by the agent.

- [ ] **Step 4: Add bootstrap summaries**

Use a fixed analysis seed and report medians plus percentile bootstrap
intervals. Preserve every individual clean session in the output.

- [ ] **Step 5: Verify GREEN**

Run analysis tests twice and assert byte-identical JSON.

---

### Task 9: Dry-run acceptance without research agents

**Files:**
- Create: `tests/accept/test_s1_harness_ablation.py`
- Create: `docs/s1-harness-ablation-operator-guide.md`

- [ ] **Step 1: Run all static gates**

```bash
uv run ruff format --check .
uv run ruff check .
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run pytest -m unit
uv run python tools/trace_check.py
uv run python tools/env_hash.py --check
```

- [ ] **Step 2: Run one manual AISLE dry attempt**

Use one development seed and the starter graph. Confirm preflight, neutral
attempt record, safety facts, artifacts, and ledger verification.

- [ ] **Step 3: Run one manual script dry attempt**

Use the same seed and starter policy. Confirm the same neutral record schema,
protected safety topology, structured policy log, and ledger verification.

- [ ] **Step 4: Run conformance acceptance**

Compare both dry attempts for goal hash, seed, environment hash, initial state
digest, guard config, verifier schema, and observation inventory. Record every
intentional interface difference.

- [ ] **Step 5: Run a zero-cost synthetic controller rehearsal**

Use fake agent streams and fake rollout adapters to exercise preparation,
budget termination, scoring, audit, exclusion, and analysis without spending
model tokens or simulator hours.

- [ ] **Step 6: Write the operator guide**

Document one-machine scheduling, pilot launch commands, stop/recovery
procedures, contamination rules, and the prohibition on viewing treatment
direction before the budget decision.

---

### Task 10: Review and freeze before pilot

**Files:**
- Modify only files required by review findings
- Create at pilot time:
  `analysis/s1-harness-ablation/protocol-lock.json`

- [ ] **Step 1: Request independent code and protocol review**

Review fairness, information parity, safety equivalence, budget authority,
held-out isolation, no-deliverable scoring, and contamination detection.

- [ ] **Step 2: Run complete affected verification**

Run unit, graph, simulation, and acceptance gates for both starter paths.

- [ ] **Step 3: Generate protocol lock**

After review passes, record SHA-256 values for:

- design and ADR;
- controller and adapters;
- both prompts;
- both starters;
- neutral schema;
- development/regression/held-out seeds;
- model/version declaration;
- budget declaration.

- [ ] **Step 4: Stop before pilot execution**

Present the frozen protocol-lock file and dry-run evidence to the owner.
Starting paid agent sessions or the pilot requires a separate explicit
authorization.
