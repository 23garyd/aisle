# ADR: S1 harness-versus-script ablation protocol

**Status:** Accepted  
**Date:** 2026-08-02

## Decision

AISLE will evaluate its S1 typed-harness treatment against an equally capable,
safe, and diagnosable monolithic Python-policy treatment. The experiment tests
harness engineering rather than whether either treatment can solve S1.

Condition A (AISLE) gives the coding agent typed dora nodes and graph,
capability registry, graph validation, per-node Arrow traces, structured
failure taxonomy, hot swap, skill registration, frozen reset/verifier/
environment/safety guard, and an experiment ledger. Condition B (Script) gives
one editable Python policy, documented callable robot APIs, Python syntax and
import preflight, structured application event logs, reusable Python helpers,
and that same frozen reset, verifier, environment, and external safety guard.
Both arms receive identical observations, robot-command authority, verifier
outcomes, videos, failure classes, and safety enforcement. The script baseline
must receive reasonable diagnostics and must not be intentionally blinded or
made unsafe. The structural treatment difference is typed composition,
pre-launch graph validation, per-node traces, hot swap, and registered skills.

The canonical starter is one incomplete but reasonable equivalent S1 solution
in `graphs/ablation_s1_starter.yaml` and `baselines/script_s1/starter.py`, using
the same oracle observations, navigation, grasp, placement, and command
implementations. It launches and may occasionally succeed, but has no explicit
grasp confirmation, bounded grasp retry, dropped-item recovery, or robust
quantity tracking. Neither arm receives the final S1 expert solution or H3
findings; conformance testing proves equivalent observations and initial action
intent for a common seed.

One experimental unit is a clean, isolated coding-agent session pinned to one
repository revision, randomly assigned one condition, unable to access other
session results, and given the identical model/version, condition-neutral goal,
token, episode, wall-time, development-seed, and held-out budgets. It receives
only condition-specific interface instructions. Run a two-session-per-condition
pilot, review instrumentation and budgets without inspecting treatment
direction, then run at least six clean sessions per condition in randomized
paired blocks. Only one simulator workload may run at a time.

Development seeds are `0..49`; the fixed regression set is `0..7`; held-out
seeds are `100..107`. The external runner alone scores held-out seeds after the
agent stops, and agents never receive held-out episode details. Both arms run
the same regression set at defined candidate checkpoints. Each session has at
most 500,000 new tokens (cache-read tokens excluded consistently), 40
development episodes, four hours of wall time, and eight externally scored
held-out episodes. The pilot may change the main-study budget only when that
decision is recorded before the main study without treatment-direction access.

Both adapters emit the neutral attempt record consisting of `attempt_id`,
SHA-256 `candidate_hash`, `preflight`, `episodes`, `failures`, `safety`,
`timing`, and `artifacts`. The external `tools/s1_harness_ablation.py`
controller owns assignment, worktree/session preparation, budgets, token
capture, attempt recording, held-out scoring, and immutable result bundles.

Primary quality is final held-out pass@1. Report `extra_item`, `missing_item`,
`dropped`, `timeout`, and placement failures; time and tokens to first
admissible development success; invalid-launch/preflight savings; regression
detection and recovery; safety; and human intervention. An invalid launch fails
before an episode result because of syntax/import, dependency, producer,
schema, command-shape, configuration, or startup failure. The shared external
safety layer records unclamped/ungated violations, clamps, `extra_item`,
collision, workspace/joint-limit attempts, and base motion with the arm
extended; an unclamped or ungated violation invalidates a session.

Each record includes condition, model/version, starting commit, runner/prompt/
starter/seed hashes, budgets, candidate and attempt hashes, preflight and
episode results, safety events, final deliverable, and dirty-tree/frozen-set
audit. Prospectively exclude only contamination by other-session results,
modified verifier/reset/environment/guard, wrong starting commit, absent token
telemetry, unpermitted operator intervention, held-out exposure, or concurrent
simulator interference. No-deliverable, budget-exhausted, and agent-abandoned
sessions receive held-out pass@1 of zero unless independently excluded.

AISLE is beneficial only with equal or better median held-out pass@1, improvement
in at least two of time/tokens/invalid-launch cost/regression cost, no increase
in `extra_item`, collision, or ungated safety failures, and clean
protocol-compliant sessions. Report all clean sessions and bootstrap intervals;
six sessions per arm does not support a strong significance claim.
