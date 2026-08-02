# S1 Harness-versus-Script Ablation Design

## Research question

With the same coding agent, robot task, starting capabilities, observations,
simulator, verifier, safety envelope, seeds, and budget, does AISLE's typed
harness produce a better engineering outcome than an equivalent monolithic
Python policy?

The experiment tests harness engineering, not whether AISLE can solve S1.

## Conditions

### Condition A — AISLE

The coding agent receives:

- typed dora nodes and graph;
- capability registry;
- graph validator;
- per-node Arrow traces;
- structured failure taxonomy;
- hot-swap support;
- skill registration;
- frozen reset, verifier, environment, and safety guard;
- experiment ledger.

### Condition B — Script

The coding agent receives:

- one editable Python policy program;
- documented callable robot APIs;
- Python syntax and import preflight;
- structured application event logs;
- reusable Python helper functions;
- the same frozen reset, verifier, environment, and external safety guard.

Both conditions receive identical task observations, robot command authority,
verifier outcomes, videos, failure classes, and safety enforcement. The script
baseline must receive reasonable diagnostics and must not be intentionally
crippled.

The treatment difference is structural harnessing: typed composition,
pre-launch graph validation, per-node traces, hot-swap, and registered skills.

## Canonical starter

Create one incomplete but reasonable S1 implementation in equivalent forms:

- `graphs/ablation_s1_starter.yaml`
- `baselines/script_s1/starter.py`

Both forms use the same underlying oracle observations, navigation, grasp,
placement, and robot-command implementations. The starter must launch and may
occasionally succeed, but initially has no explicit grasp confirmation,
bounded grasp retry, dropped-item recovery, or robust quantity tracking.

Neither arm receives the existing final S1 expert solution or prior H3
findings. A conformance test must prove equivalent observations and initial
action intent on the same seed.

## Experimental unit and randomization

One unit is a fresh coding-agent session with:

- a pinned repository revision;
- one randomly assigned condition;
- a clean isolated worktree;
- no access to other session results;
- the same model and exact model version;
- condition-neutral goal text;
- condition-specific interface instructions only;
- identical token, episode, wall, development-seed, and held-out budgets.

Run a two-session-per-condition pilot. After instrumentation and budget review,
run at least six clean sessions per condition in randomized paired blocks.
Only one simulator workload may run on the workstation at a time.

## Seeds and scoring

- Development pool: `0..49`.
- Fixed regression set: `0..7`.
- Held-out set: `100..107`.
- The external runner scores held-out seeds after the agent stops.
- Agents never receive held-out episode details.
- Both conditions run the same regression set at the same defined candidate
  checkpoints.

A session producing no deliverable receives held-out pass@1 of zero.

## Initial budget

Per session:

- 500,000 new tokens;
- at most 40 development episodes;
- at most four hours wall time;
- eight externally scored held-out episodes.

Cache-read tokens are excluded consistently. The pilot may justify changing
the main-study budget only if the decision is made without inspecting
treatment direction and is recorded before the main study.

## Neutral result interface

Both condition adapters return:

```json
{
  "attempt_id": "string",
  "candidate_hash": "sha256",
  "preflight": {
    "ok": true,
    "errors": [],
    "wall_s": 0.0
  },
  "episodes": [],
  "failures": {},
  "safety": {},
  "timing": {},
  "artifacts": {}
}
```

An external controller, `tools/s1_harness_ablation.py`, owns condition
assignment, session preparation, budgets, token capture, attempt recording,
held-out scoring, and immutable result bundles. Condition adapters translate
AISLE or script execution into the neutral schema.

## Metric definitions

### Held-out success

Primary quality metric: final pass@1 over seeds `100..107`. Also report
`extra_item`, `missing_item`, `dropped`, `timeout`, and placement failures.

### Time to first success

Wall time from session start to completion of the first admissible successful
development episode. Separately record agent time, preflight time, simulator
time, and waiting time.

### Tokens to first success

Cumulative new tokens consumed when the first admissible successful episode
completes.

### Invalid launch

An attempted candidate is invalid when it fails before producing an episode
result because of syntax/import errors, missing dependencies, missing
producers, schema mismatches, invalid command shapes, configuration errors, or
startup failure.

Report invalid candidates, errors caught before launch, simulator episodes
avoided, and wall time spent detecting invalid attempts.

### Regression

After a candidate passes any fixed regression-set seed, a later candidate
regresses if that seed fails. Report regressing candidate changes, lost
passing seeds, detection time, and recovery.

### Safety

The same external safety layer measures:

- unclamped/ungated violations;
- guard clamp count;
- `extra_item`;
- collision;
- workspace or joint-limit attempts;
- base motion while the arm is extended.

An unclamped or ungated safety violation invalidates the session.

## Integrity and exclusion

Every session record includes:

- condition;
- model and version;
- starting commit;
- runner, prompt, starter, and seed hashes;
- budgets;
- every candidate and attempt hash;
- preflight outcomes;
- episode results;
- safety events;
- final deliverable;
- dirty-tree and frozen-set audit.

Exclude a session prospectively for:

- access to another session's results;
- modified verifier, reset, environment, or guard;
- wrong starting commit;
- missing token telemetry;
- unpermitted operator intervention;
- held-out exposure;
- concurrent simulator interference.

No-deliverable, budget-exhausted, and agent-abandoned sessions are scored zero
unless an exclusion rule independently applies.

## Implementation sequence

1. Record a repository ADR for the protocol.
2. Define and test the neutral result schema.
3. Implement the external budget and provenance ledger.
4. Implement the protected script execution adapter.
5. Create equivalent AISLE and script starters.
6. Add observation, command, seed, and verifier conformance tests.
7. Implement both condition adapters.
8. Implement regression checkpoint evaluation.
9. Implement external held-out scoring.
10. Implement contamination and frozen-drift audits.
11. Run manual dry runs without a coding agent.
12. Run one short agent session per condition.
13. Audit information and opportunity parity.
14. Freeze the protocol, runner, starter, and prompt hashes.
15. Run the pilot.
16. Make any budget decision without viewing treatment direction.
17. Run the randomized main study.
18. Generate machine-derived tables, intervals, and session inventory.

## Analysis

Report individual sessions and distributions for:

- held-out pass@1;
- probability of producing a working candidate;
- time, episodes, and tokens to first success;
- invalid attempts and preflight savings;
- regression count, detection time, and recovery;
- safety outcomes;
- human interventions.

With six sessions per arm, uncertainty will remain wide. Use bootstrap
intervals, publish all clean session outcomes, and avoid an unsupported
significance claim.

## Decision criterion

Call the AISLE harness beneficial only if it:

1. achieves equal or better median held-out pass@1;
2. improves at least two of time, tokens, invalid-launch cost, or regression
   cost;
3. does not increase `extra_item`, collision, or ungated safety failures;
4. is supported by clean, protocol-compliant sessions.

This criterion prevents a faster but less capable or less safe system from
being labeled an improvement.
