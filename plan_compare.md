# AISLE Harness Engineering Comparison Plan

## Central distinction

ASPIRE, ENPIRE, and Harness VLA primarily propose ways to improve robot
behavior. AISLE asks how the surrounding engineering harness changes the
reliability, safety, speed, and scientific credibility of that improvement.

AISLE is not intended to win by inventing another planner or policy. Its
potential contribution is the infrastructure in which planners, coding agents,
VLAs, and skill-learning methods operate.

## Policy versus harness

A robot policy answers:

> Given this observation and goal, what should the robot do?

A robot harness answers:

> How is that policy connected, constrained, observed, evaluated, repaired,
> replaced, and compared?

Without a strong harness, a coding agent may produce one Python script
containing perception, planning, motion, retries, logging, and evaluation. When
it fails, the agent sees only that the task failed and must infer which
subsystem caused it.

AISLE separates the behavioral system into typed nodes:

```text
perception
   ↓
task planner
   ↓
navigation / grasp planner
   ↓
motion executor
   ↓
budget guard
   ↓
robot or simulator
```

Protected infrastructure runs alongside it:

```text
reset
verifier
trace recorder
environment hash
failure classifier
experiment ledger
```

The claim to test is that this separation makes agentic robot engineering more
effective than editing a monolithic script.

## Relationship to related systems

### ENPIRE

ENPIRE is closest to AISLE's outer research loop. It frames physical
autoresearch as a coding agent that proposes hypotheses, edits robot policies,
runs experiments, and reads results.

AISLE can provide the execution substrate beneath an ENPIRE-style agent:

```text
ENPIRE-style research agent
             ↓
AISLE validator and experiment harness
             ↓
dora nodes
             ↓
Genesis or physical robot
```

The difference in emphasis:

- ENPIRE: can an autonomous agent improve a real robot policy?
- AISLE: does typed middleware make that improvement faster, safer, more
  diagnosable, and more reproducible?

Reference: [ENPIRE](https://arxiv.org/abs/2606.19980).

### ASPIRE

ASPIRE focuses on continual learning through robot-program repair and a
growing skill library. Its execution engine exposes detailed traces; validated
repairs become reusable skills, while evolutionary search generates tasks and
programs.

AISLE's relationship is:

```text
ASPIRE-style skill discovery
             ↓
AISLE skill registration and evalcards
             ↓
typed capability manifests
             ↓
validated reuse in later graphs
```

The difference in emphasis:

- ASPIRE: how can successful repairs accumulate into transferable skills?
- AISLE: how can skill composition and reuse be typed, validated,
  safety-gated, and experimentally audited?

AISLE's H3 experiment attempted to show this advantage but did not meet its
target. The current skill mechanism has not yet proved that persistent
libraries make later tasks faster.

Reference: [ASPIRE](https://arxiv.org/abs/2607.00272).

### Harness VLA

Harness VLA treats a frozen VLA as a retryable contact-rich primitive. A
memory-guided planner learns when and how to invoke the VLA, while analytic
primitives handle grounding, staging, transport, navigation, and release.

AISLE could host this architecture:

```text
memory-guided planner node
          ↓
analytic navigation/staging nodes
          ↓
frozen VLA node for contact-rich motion
          ↓
AISLE budget guard
          ↓
robot
```

The distinction:

- Harness VLA: a specific behavioral harness around a frozen VLA.
- AISLE: a general systems harness for composing and evaluating many policy
  types, including Harness VLA.

AISLE can add enforcement and evidence around the VLA:

- Which input/output schemas are allowed?
- Is the VLA authorized for this robot embodiment?
- Are its actions within joint and workspace limits?
- Did it violate timing or bandwidth budgets?
- Can it access privileged oracle state?
- Which version executed?
- Can the node be replaced independently?
- Did the change improve held-out performance?

Reference: [Harness VLA](https://arxiv.org/abs/2607.08448).

## AISLE's potential uniqueness

### 1. Typed composition

Each node declares inputs, outputs, Arrow schemas, rates, robot embodiment,
capabilities, and source/package identity. The validator can reject an invalid
system before spending a simulation or physical-robot episode.

This makes composition machine-checkable by the coding agent.

### 2. Structural safety

Safety is outside the policy:

- Motion must pass through the budget guard.
- Oracle state cannot be routed into policy nodes.
- Frozen environment, verifier, and reset code is hash-checked.
- Unsafe graph topology is rejected before launch.

A policy cannot merely promise to be safe; the graph structure enforces part
of the safety envelope.

This resembles the projection concept in
[Harness Engineering for Physical AI](https://arxiv.org/abs/2606.09416),
which proposes robot middleware as the layer that composes control, computing,
and communication enforcement.

### 3. Failure localization

A monolithic run may report:

```text
task failed
```

AISLE can report:

```text
graph launch succeeded
target pose arrived at 15 Hz
grasp goal began at t=4.2
gripper closed without object displacement
failure = never_grasped
```

This reduces the coding agent's search space. It can repair the grasp or
confirmation node without rewriting navigation and verification.

### 4. Controlled experimentation

AISLE records:

- Git revision;
- graph hash;
- environment hash;
- dependency fingerprint;
- platform and backend;
- seed;
- episode result and failure type;
- trace and video;
- idea and hypothesis;
- token, episode, and wall budget.

This turns “the agent improved the robot” into an auditable experiment.

### 5. Replaceable units

Hot-swap allows a planner or controller to be replaced without restarting the
complete graph. H4 measured a T0 median of 32.4 seconds for hot-swap versus
41.8 seconds for relaunch. The larger opportunity is in scenes whose
initialization takes minutes.

### 6. Policy independence

An AISLE node can contain:

- analytic control;
- motion planning;
- coding-agent-generated code;
- a VLM;
- a frozen VLA;
- an RL policy;
- a Harness VLA planner;
- an ASPIRE-discovered skill.

The same validation, safety, tracing, and evaluation machinery remains around
all of them.

## The experiment AISLE must run

The important question is not merely:

> Can AISLE solve S1?

The important question is:

> Given the same coding agent, robot task, starting capabilities, and
> compute/episode budget, does the AISLE harness produce a better engineering
> outcome than an equivalent monolithic-script environment?

This has not yet been demonstrated strongly enough.

## Matched harness-versus-script study

### Condition A — AISLE harness

The coding agent receives:

- typed node registry;
- graph validator;
- structured failure taxonomy;
- per-topic traces;
- hot-swappable nodes;
- frozen verifier and reset;
- skill registration;
- experiment ledger.

### Condition B — Script baseline

The same agent receives:

- the same simulator;
- the same robot APIs;
- the same task;
- the same verifier outcome;
- the same observations;
- the same token, wall-time, and episode budget;
- one editable Python policy script.

The baseline must not be intentionally crippled. It should receive reasonable
logs and the same non-privileged observations. The tested difference should be
structural harnessing, not basic usability.

## Metrics

| Metric | Why it matters |
|---|---|
| Held-out pass@1 | Final behavior quality |
| Time to first success | Engineering speed |
| Episodes to first success | Robot/sample efficiency |
| Tokens to first success | Agent efficiency |
| Invalid launches | Composition reliability |
| Failure-localization accuracy | Diagnostic value |
| Wrong/extra item rate | Safety outcome |
| Ungated violations | Structural safety |
| Regression rate | Whether fixes break other components |
| Reuse on S2/S3 | Skill transfer |
| Restart versus hot-swap time | Iteration overhead |
| Human interventions | Autonomy |
| Reproducible rerun rate | Scientific reliability |

A useful result would look like:

```text
Same coding agent, same S1 task, same 40-hour budget

Script condition:
- 0.50 held-out pass@1
- 34 episodes to first success
- 3 regressions
- 2 extra-item events

AISLE condition:
- 0.80 held-out pass@1
- 15 episodes to first success
- 1 regression
- 0 extra-item events
```

Only a matched experiment of this kind can support a strong claim that harness
engineering improves robot engineering.

## Honest current evidence

- H1: typed composition produced schema-valid graphs, but launch success
  missed its target because dependencies were absent. This led to
  `INSTALL_MISSING` validation.
- H2: the iterative agent loop produced high-performing systems.
- H3: skill accumulation did not meet its target under the tested budgets.
- H4: hot-swap was faster than relaunch at T0, but the evidence was limited.
- H5: wrong-object safety remained strong in the principal evidence.

AISLE has demonstrated useful mechanisms, but it has not yet proved the full
headline claim:

> Typed dataflow harness engineering is superior to script-level agentic robot
> development.

## Recommended research architecture

Use ASPIRE, ENPIRE, and Harness VLA as workloads inside AISLE:

```text
ENPIRE-style coding agent
        ↓
ASPIRE-style skill discovery and memory
        ↓
Harness VLA contact primitive where learned control is valuable
        ↓
AISLE typed composition and middleware enforcement
        ↓
Genesis or physical robot
```

Then ablate the AISLE layer:

```text
same agent + same methods + AISLE
versus
same agent + same methods + monolithic script
```

## Recommended next experiment

Before investing heavily in realistic perception or physical hardware, run
one rigorous S1 harness ablation:

> Does AISLE reduce time, tokens, invalid launches, regressions, and safety
> failures while reaching equal or better held-out S1 success than a script
> baseline?

If yes, AISLE has a distinctive systems contribution: not a better robot
brain, but a better engineering environment in which robot brains can be
safely and autonomously improved.

