# Native Agent–Simulator Isolation Design

**Status:** Approved design  
**Date:** 2026-08-02  
**Parent experiment:** `2026-08-02-s1-harness-ablation-design.md`

## Purpose

The S1 harness-versus-script experiment requires the external controller to be
the sole authority that launches dora, Genesis, and CUDA simulator workloads.
Process-name monitoring and run-directory audits are insufficient when a
research agent runs under the same Linux user with unrestricted filesystem,
device, and process access: it can import Genesis directly, rename a launcher,
or invoke the GPU without leaving the expected controller artifacts.

Research agents therefore run in a native Linux Bubblewrap sandbox. The
trusted campaign controller and simulator remain outside that sandbox and use
the workstation's existing CUDA, PyTorch, Genesis, and dora installation.
This design does not use Docker and must not install or modify the NVIDIA
driver, CUDA toolkit, or host PyTorch environment.

## Security Property

During an active research session:

1. the agent can edit only its assigned session worktree and approved scratch
   locations;
2. the agent has network access for its Claude or Codex model connection;
3. the agent cannot see NVIDIA device nodes, the host process namespace,
   Docker/LXD control sockets, unrelated worktrees, the simulator environment,
   or executable dora/Genesis launch surfaces;
4. every development or regression attempt crosses an authenticated,
   session-scoped Unix socket into a trusted controller process;
5. only that controller process may reserve episode capacity and launch the
   assigned adapter, dora, Genesis, or CUDA work;
6. held-out seeds remain unavailable until the sandboxed agent has stopped.

The controller must refuse to start a research session when it cannot establish
and verify all required isolation properties. Detection-only fallback is not
permitted for a clean experimental session.

## Architecture

### Trusted host

The trusted side contains:

- the campaign controller;
- the attempt broker;
- the pinned session source used for simulator execution;
- the existing CUDA-enabled project environment;
- dora, Genesis, verifier, reset, budget guard, adapters, and rollout runners;
- session ledgers, attempt records, manifests, audit evidence, and held-out
  scoring state.

These components run outside the agent namespace. The agent-facing worktree
must not contain an executable controller or capability secret that can be
modified and then trusted.

### Sandboxed agent

Bubblewrap creates a fresh mount and PID namespace for each session. The
sandbox receives:

- a read/write bind mount for the assigned worktree;
- approved temporary and cache directories;
- a read-only minimal agent environment containing editing, unit-test,
  validation, git, Claude/Codex, and attempt-client dependencies;
- the session attempt socket mounted at a fixed sandbox path;
- required network access.

The sandbox does not receive:

- `/dev/nvidia*` or NVIDIA capability devices;
- Docker, LXD, containerd, or similar daemon sockets;
- the host `/proc` tree;
- the host CUDA-enabled project environment;
- dora CLI/runtime launch executables;
- Genesis simulation packages;
- other repository worktrees or campaign sessions;
- controller ledgers, held-out seed files, or the raw capability secret.

The minimal agent environment is separate from the host simulation
environment. Creating it must not mutate or reinstall the host CUDA/PyTorch
environment.

### Attempt client and broker

The sandbox contains a small immutable attempt client. The client sends one
JSON request over the mounted Unix socket. Authentication uses a
controller-generated, session-scoped capability bound to:

- session ID;
- condition;
- broker instance;
- pinned controller, adapter, runner, and frozen-source hashes;
- socket inode and creation time;
- session start and expiry;
- a monotonically increasing request nonce.

The raw capability is held by the broker. The sandbox receives only a
short-lived client credential sufficient to authenticate requests for its own
session. Credentials are non-transferable across sessions and are revoked when
the agent stops.

For every request the broker:

1. authenticates the session and nonce;
2. validates candidate path containment and current candidate hash;
3. validates unique development seeds `0..49` and regression seeds `0..7`;
4. rejects held-out seeds `100..107`;
5. acquires global simulator authority;
6. atomically admits and reserves the requested episode capacity;
7. invokes only the assigned condition adapter from the pinned trusted source;
8. records the canonical `AttemptResult`, manifest, safety evidence, and
   settlement;
9. returns a neutral JSON response.

An incomplete or crashed attempt is charged conservatively from its admission.

## Session Lifecycle

### Prepare

`prepare` creates the pinned session worktree, copies only the assigned starter
surface, records frozen provenance, and creates all required controller
artifacts. It also records the sandbox policy and the hashes of the trusted
controller, broker, attempt client, agent environment, adapters, runners, seed
domains, and Bubblewrap executable.

### Run

`run`:

1. verifies the pinned worktree and trusted hashes;
2. creates the broker socket and credential;
3. verifies the Bubblewrap feature set;
4. starts the broker outside the sandbox;
5. starts Claude or Codex inside the sandbox;
6. monitors token, wall, process-tree, socket, and attempt activity;
7. terminates the complete sandbox process tree at any budget or policy
   violation;
8. revokes credentials and removes the socket after agent termination;
9. verifies that no session process or broker-owned simulator remains.

The agent prompt exposes the attempt-client command but no trusted host path.

### Score

`score` acquires a stable interprocess lock, reloads session state, and
atomically writes a non-retryable scoring admission containing a fresh nonce
before any held-out launch. The trusted host adapter executes held-out seeds
exactly once from the pinned session source. A crash after admission remains a
terminal invalid scoring result and cannot be retried.

### Audit

`audit` fails a session for:

- missing or changed sandbox policy evidence;
- missing namespace/device/socket probes;
- agent visibility of prohibited devices, sockets, packages, paths, or host
  processes;
- an attempt not admitted by the broker;
- missing, duplicated, or mismatched admission/result/settlement/manifest
  records;
- episode spend exceeding the session ceiling, charging unsettled admissions
  at their requested capacity;
- capability reuse, replay, or cross-session use;
- agent-time held-out exposure;
- scoring without a locked pre-admission nonce;
- concurrent simulator authority;
- surviving sandbox, broker, dora, or Genesis processes;
- frozen-source, prompt, runner, adapter, candidate, or provenance drift.

## Failure Handling

- Missing Bubblewrap or unsupported namespaces: refuse session startup.
- Agent environment contains simulator/GPU launch surfaces: refuse startup.
- Broker startup or socket mount failure: refuse startup.
- Broker disconnect after admission: charge the reservation and record an
  incomplete attempt.
- Authentication, replay, seed, path, or hash failure: reject without launch
  and record a policy violation.
- Token or wall budget exceeded: return controller failure and terminate the
  entire sandbox process group.
- Sandbox escape probe succeeds: terminate and exclude the session.
- Cleanup cannot prove zero surviving session processes: mark the session
  contaminated.
- Scoring crashes after admission: retain terminal invalid held-out state.

Every controller and attempt-client CLI response is exactly one JSON object on
stdout and exits zero if and only if `"ok": true`.

## Verification

### Unit and synthetic tests

Tests use fake agents and adapters to prove:

- correct Bubblewrap argv and mount policy;
- finite wall/token/episode ceilings;
- immutable condition and session identity;
- authenticated attempt admission and nonce monotonicity;
- replay and cross-session rejection;
- candidate containment and hash checks;
- development/regression seed admission and held-out rejection;
- reservation before launch and conservative crash charging;
- one-to-one attempt, manifest, and settlement correlation;
- locked, crash-safe, exactly-once held-out admission;
- JSON-only CLI help and error paths;
- complete process-tree cleanup.

### Native isolation acceptance

Harmless probes inside the actual sandbox must confirm:

- assigned worktree editing succeeds;
- approved unit tests, validation, git, and network access succeed;
- unrelated worktrees and controller artifacts are inaccessible;
- host processes are not visible;
- `/dev/nvidia*` and container daemon sockets are absent;
- importing Genesis and executing dora fail;
- direct simulator launch fails;
- the authenticated attempt client succeeds;
- a copied or replayed credential fails.

### Bounded CUDA acceptance

One development-seed attempt is issued from the sandbox through the broker.
The trusted host uses the already-installed CUDA environment and RTX GPU.
Evidence must show:

- the sandbox has no GPU access;
- the broker owns simulator authority;
- the adapter and wrapper come from the pinned trusted source;
- the attempt is admitted before launch and settled afterward;
- safety evidence and canonical result are recorded;
- all dora, Genesis, broker, and sandbox processes are cleaned up.

No research-agent pilot, paid campaign, or held-out campaign is permitted
during implementation or acceptance.

## Alternatives Rejected

### Dedicated Linux user

A dedicated account can provide strong separation, but requires persistent
host account, group, filesystem, and device administration. It remains a
future hardening option. Bubblewrap provides sufficient per-session native
isolation without those host changes.

### systemd-only restrictions

User-systemd is useful for cgroups, timeouts, and cleanup, but is insufficient
alone for mount, device, and package isolation. It may supervise Bubblewrap but
does not replace it.

### Detection-only monitoring

Process-name checks, run-directory audits, and prompt rules cannot prevent an
unrestricted same-user agent from importing or renaming simulator launch code.
Detection-only sessions cannot support the claim that the controller is the
sole simulator authority and are excluded from the clean experiment.

## Completion Boundary

This design amends Task 7 and the later dry-run/protocol-lock work. It does not
authorize pilot or main-study execution. After implementation and independent
review, the owner must separately authorize any paid research-agent session.
