# AISLE Plan 1 — Autonomous S1 Retail Improvement

## Current repository state

The CUDA backend and demo work is committed locally:

```text
f0a7cab feat: enable automatic CUDA simulation backend
```

Local `main` is one commit ahead of `origin/main`. Nothing has been pushed.

Simply leaving the simulator running will not improve robot behavior. Genesis
provides execution and evidence, not learning. Improvement requires a research
agent to edit policy and planner nodes, run rollouts, inspect failures, and
preserve successful behavior as reusable skills.

## First improvement scenario

Start with **S1 retail order picking** using the mobile base and Franka arm.

The robot must:

1. Receive an order for two product types and quantities.
2. Navigate from the counter to the correct shelf locations.
3. Pick the correct products.
4. Return to the counter.
5. Deliver everything requested and nothing extra.

S1 is preferable to another T0 campaign because T0 is already solved. S1
exercises the project's main research claims:

- mobile navigation;
- long-horizon planning;
- multiple picks and deliveries;
- exact product and quantity selection;
- recovery after failures;
- reusable skills that can transfer to S2 and S3.

## Campaign objective

> Improve the S1 mobile order-picking graph to at least 0.80 pass@1 on
> development seeds and at least 0.75 on eight held-out seeds, with zero
> `extra_item` failures and zero ungated motion violations.

Secondary metrics:

- Reduce `missing_item`, `dropped`, and `timeout`.
- Record time to first successful episode.
- Record tokens and episodes required.
- Preserve deterministic initial scene state.
- Extract at least one reusable skill used by the final graph.

Earlier H3 S1 results were only 0.375 and 0.500 held-out, so this is a
meaningful improvement target.

## Technology approach

AISLE remains the central runtime and evaluation harness. ENPIRE and ASPIRE
provide complementary research patterns rather than replacing AISLE.

### ENPIRE-style outer loop

Use an external coding agent such as Codex or Claude to operate the experiment:

```text
inspect graph and traces
        ↓
form one hypothesis
        ↓
modify planner or skill node
        ↓
validate typed graph
        ↓
run a small seed batch
        ↓
inspect failure taxonomy
        ↓
keep, revise, or reject the idea
```

This hypothesis-driven loop is the mechanism that produces continued
improvement.

### ASPIRE-style skill accumulation

When the agent discovers a successful behavior, extract it into a versioned
skill with evaluation evidence.

Useful initial skills include:

- `navigate-to-zone`
- `shelf-approach`
- `pick-retail-item`
- `return-to-counter`
- `place-on-counter`
- `recover-dropped-item`
- `placement-controller`

These skills form the transfer channel from S1 to:

- S2: shelf restocking;
- S3: detection and correction of misplaced products.

### AISLE's role

AISLE provides the structural guarantees for the research loop:

- typed dora graph composition;
- pre-launch validation;
- frozen verifier and environment;
- guarded motion;
- structured failure taxonomy;
- seeded rollouts;
- Arrow traces and videos;
- held-out evaluation;
- idea logging and campaign budgets;
- live node replacement.

The recommended combination is:

```text
Codex or Claude research agent
        +
ENPIRE-style hypothesis loop
        +
ASPIRE-style persistent skills
        +
AISLE typed runtime, safety, and evaluation
        +
Genesis CUDA simulation
```

## Perception strategy

Use oracle perception first. Do not add a VLM during the initial campaign.

The first campaign should improve planning, navigation, manipulation, and
recovery without mixing in perception errors. Once oracle S1 reliably meets
the target, introduce the realistic rung:

- OCR or VLM for reading the order slip;
- product detection for shelf items;
- planogram-aware shelf localization;
- a misplacement detector for S3.

A compact VLM may later provide semantic perception, but it should not control
the low-level robot loop. It should produce structured observations consumed
by deterministic planners and guarded controllers.

## Staged roadmap

### Phase 1 — Oracle S1 order picking

Goal: at least 0.80 development pass@1, at least 0.75 held-out pass@1, and
zero `extra_item`.

### Phase 2 — Stabilize reusable skills

Goal: ensure the final S1 graph actually uses registered, evaluated skills.

### Phase 3 — S2 restocking transfer

Goal: reuse navigation, grasp, and placement skills while learning only the
restocking-specific behavior.

### Phase 4 — S3 misplaced-item correction

Goal: reuse S1/S2 skills with minimal new policy code.

### Phase 5 — Realistic perception

Goal: replace oracle order and product inputs with OCR, VLM, and detection
nodes while retaining the oracle verifier as the reference.

### Phase 6 — Physical robot

Goal: reuse the same typed graph, capability contracts, safety guard, and
evaluation structure on real hardware.

## First improvement tree

The research agent should investigate:

1. Task-planner correctness: finish all requested quantities.
2. Navigation/manipulation sequencing: do not move the base while the arm is
   extended.
3. Reliable grasp confirmation before transport.
4. Counter placement that prevents drops.
5. Recovery when an item is not grasped or is dropped.
6. Avoid moving any non-ordered item to the counter.

The first hypothesis should be:

> Explicit grasp confirmation plus one bounded retry before navigation will
> reduce `missing_item` and `dropped` without increasing `extra_item`.

Test this hypothesis on four development seeds before spending a larger
campaign budget.

## Infrastructure prerequisites

Before starting a long autonomous run:

1. Make CUDA PyTorch reproducible through a constitution-approved optional
   `cuda` extra. The current CUDA wheel is a local `.venv` override.
2. Record backend and GPU provenance in every run manifest:
   - Genesis backend;
   - GPU model;
   - NVIDIA driver;
   - PyTorch version;
   - CUDA runtime version;
   - peak GPU memory.
3. Establish a clean S1 baseline on the selected development and held-out
   seeds.
4. Keep one simulator workload active at a time.

## Recommended first autonomous campaign

Improve oracle S1 mobile order picking under the AISLE research contract, then
promote successful navigation, grasp, recovery, and placement behaviors into
reusable skills for S2 and S3.

