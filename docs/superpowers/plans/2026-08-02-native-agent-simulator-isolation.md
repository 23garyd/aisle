# Native Agent–Simulator Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the S1 campaign controller the enforceable sole authority for dora, Genesis, and CUDA attempts while research agents run natively inside a restricted Bubblewrap sandbox.

**Architecture:** A trusted host controller owns a session-scoped Unix-socket attempt broker and the CUDA simulation environment. Claude or Codex runs in a Bubblewrap mount/PID/device namespace with network access, its assigned worktree, a separate non-simulation Python environment, and a read-only attempt client; it cannot see NVIDIA devices, simulator packages, dora launchers, host processes, container sockets, or other worktrees.

**Tech Stack:** Python 3.13, Bubblewrap, Linux namespaces, Unix domain sockets, uv, pytest, existing AISLE controller/adapters/ledgers, existing host CUDA environment.

## Global Constraints

- Do not use Docker or create a root daemon.
- Do not install or modify the NVIDIA driver, CUDA toolkit, or host PyTorch environment.
- The host controller may reuse the existing CUDA-enabled project environment.
- The agent environment must be separate and must omit the `sim` extra, Genesis, dora runtime, torch, and NVIDIA packages.
- The sandbox retains network access for Claude/Codex.
- The sandbox receives no `/dev/nvidia*`, host `/proc`, container daemon socket, unrelated worktree, trusted controller source, raw held-out seeds, or host simulation environment.
- Every development/regression simulator attempt must cross the authenticated broker.
- Development seeds are `0..49`; regression seeds are `0..7`; held-out seeds `100..107` are rejected while the agent is active.
- The controller remains the sole authority for 500,000 new tokens, 40 development episodes, four wall-clock hours, and one simulator workload.
- Every CLI emits one JSON object to stdout and exits zero if and only if `"ok": true`.
- Missing Bubblewrap, incomplete namespace isolation, missing broker authentication, or failed cleanup must refuse or contaminate the session.
- Do not run a paid research-agent session, pilot, main study, or held-out campaign during implementation.

---

### Task 1: Native sandbox policy and harmless probes

**Files:**
- Create: `src/aisle/harness/native_sandbox.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Produces:
  `SandboxPolicy(worktree: Path, agent_env: Path, runtime_dir: Path, attempt_client: Path, agent_executable: Path, credential_mounts: tuple[Path, ...])`
- Produces:
  `build_bwrap_argv(policy: SandboxPolicy, command: list[str], env: dict[str, str]) -> list[str]`
- Produces:
  `verify_sandbox_probe(result: dict) -> tuple[bool, tuple[str, ...]]`
- Produces:
  `sandbox_probe_command() -> list[str]`
- Consumed by: Task 3 controller integration

- [ ] **Step 1: Add failing Bubblewrap policy tests**

Add unit tests that construct a temporary policy and assert the generated argv contains:

```text
bwrap
--die-with-parent
--new-session
--unshare-pid
--unshare-ipc
--unshare-uts
--proc /proc
--dev /dev
--clearenv
```

Assert it read/write binds only the assigned worktree and approved scratch,
read-only binds the agent environment, attempt client, runtime socket
directory, required system libraries/certificates, the selected Claude/Codex
executable, and explicit credential mounts.

Assert it does not bind:

```text
/dev/nvidia0
/dev/nvidiactl
/var/run/docker.sock
/run/docker.sock
/run/containerd/containerd.sock
the host project .venv
the trusted controller source directory
the repository .worktrees parent
```

Use distinct fixture paths so a substring assertion cannot pass accidentally.

- [ ] **Step 2: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH="$PWD/src:$PWD" \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k native_sandbox -q
```

Expected: import failure because `aisle.harness.native_sandbox` does not exist.

- [ ] **Step 3: Implement immutable policy validation**

Use frozen dataclasses. Resolve every bind source before argv construction and
reject:

```python
FORBIDDEN_SOURCES = (
    Path("/dev/nvidia0"),
    Path("/dev/nvidiactl"),
    Path("/var/run/docker.sock"),
    Path("/run/docker.sock"),
    Path("/run/containerd/containerd.sock"),
)
```

Reject any bind source that is the host simulation environment, the trusted
controller checkout, or an ancestor containing unrelated worktrees. Require
the runtime directory to be owned by the current UID and mode `0700`; require
the socket and client to be outside the writable worktree.

Build argv only as a list. Use a synthetic `/dev`, new `/proc`, an empty
temporary home, and explicit environment variables:

```text
HOME=/agent-home
PATH=/agent-env/bin:/usr/bin:/bin
PYTHONNOUSERSITE=1
AISLE_ABLATION_SOCKET=/run/aisle/attempt.sock
AISLE_ABLATION_CLIENT=/opt/aisle/attempt-client
```

Pass only the chosen vendor's required API/auth variables, network proxy
variables, locale, and certificate variables. Do not inherit arbitrary host
environment variables.

- [ ] **Step 4: Add and implement the sandbox probe contract**

The probe emits one JSON object containing:

```json
{
  "worktree_write": true,
  "network_dns": true,
  "host_process_visible": false,
  "nvidia_visible": false,
  "docker_socket_visible": false,
  "genesis_importable": false,
  "dora_executable": false,
  "other_worktree_visible": false,
  "attempt_socket_visible": true
}
```

`verify_sandbox_probe` requires the exact keys and exact booleans. Unknown or
missing keys fail closed. The probe must use harmless filesystem/import/path
checks and DNS resolution only; it must not launch a simulator or contact a
model API.

- [ ] **Step 5: Verify GREEN and commit**

Run the Step 2 test, Ruff on the two affected files, and `git diff --check`.
Expected: all sandbox-policy/probe tests pass.

Commit:

```bash
git add src/aisle/harness/native_sandbox.py tests/unit/test_s1_harness_ablation.py
git commit -m "feat: add native research-agent sandbox policy"
```

---

### Task 2: Separate non-simulation agent environment

**Files:**
- Create: `tools/build_s1_agent_env.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Produces:
  `AgentEnvironmentSpec(python: str, lock_sha256: str, distributions: tuple[str, ...], executables: tuple[str, ...])`
- Produces:
  `build_agent_environment(repo: Path, destination: Path, *, runner: Callable) -> dict`
- Produces:
  `inspect_agent_environment(destination: Path) -> AgentEnvironmentSpec`
- CLI:
  `python tools/build_s1_agent_env.py --repo <path> --out <path>`

- [ ] **Step 1: Add failing environment-command and inspection tests**

With a fake runner, assert the builder invokes uv against the repository lock
without the `sim` extra and without modifying the host project environment:

```text
uv sync --frozen --no-extra sim --group dev
```

The environment target must be an explicit controller-owned path outside the
session worktree. Tests must reject inspection results containing any of:

```text
genesis-world
dora-rs
torch
nvidia-*
```

or executables named:

```text
dora
genesis
```

Assert the result records hashes of `uv.lock`, the builder, Python executable,
installed distribution inventory, and executable inventory.

- [ ] **Step 2: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH="$PWD/src:$PWD" \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k agent_environment -q
```

Expected: import failure for `tools.build_s1_agent_env`.

- [ ] **Step 3: Implement builder and strict inspection**

Run uv with an explicit task-specific `UV_PROJECT_ENVIRONMENT` pointing at the
destination. Never use or replace the host `.venv`. After sync, inspect
distributions using the destination Python's `importlib.metadata` and inspect
`bin/` without importing forbidden packages.

The CLI returns:

```json
{
  "ok": true,
  "environment": "/absolute/path",
  "lock_sha256": "64 hex",
  "inventory_sha256": "64 hex"
}
```

On forbidden packages, executable drift, sync failure, or malformed inventory,
return `"ok": false` and a stable code.

- [ ] **Step 4: Add idempotence and host-preservation tests**

Assert a matching destination is reused without syncing. A changed lock or
inventory forces a rebuild only after moving the prior destination to a
task-specific quarantine path; never delete or mutate the host `.venv`.

- [ ] **Step 5: Verify GREEN and commit**

Run the focused tests, Ruff, trace check, and `git diff --check`.

Commit:

```bash
git add tools/build_s1_agent_env.py tests/unit/test_s1_harness_ablation.py
git commit -m "feat: build isolated S1 agent environment"
```

---

### Task 3: Trusted broker, immutable client, and sandboxed controller run

**Files:**
- Create: `tools/s1_harness_attempt_client.py`
- Modify: `tools/s1_harness_ablation.py`
- Modify: `src/aisle/harness/ablation_adapters.py`
- Modify: `src/aisle/harness/script_preflight.py`
- Modify: `src/aisle/harness/script_preflight_worker.py`
- Modify: `src/aisle/harness/script_rollout.py`
- Modify: `tests/unit/test_s1_harness_ablation.py`

**Interfaces:**
- Attempt client CLI:
  `python /opt/aisle/attempt-client --seeds <csv>`
- Broker request:

```json
{
  "protocol": 1,
  "session_id": "P01-A",
  "condition": "aisle",
  "nonce": 1,
  "credential": "opaque",
  "candidate_relpath": "candidate/graph.yaml",
  "candidate_sha256": "64 hex",
  "seeds": "0,1"
}
```

- Produces:
  `AttemptBroker.start() -> BrokerEndpoint`
- Produces:
  `AttemptBroker.authorize(request: dict) -> AuthorizedAttempt`
- Consumes: `build_bwrap_argv`, inspected agent environment, existing adapters
- Preserves public controller commands: `prepare`, `run`, `score`, `audit`

- [ ] **Step 1: Add failing client/broker authentication tests**

Test exact protocol keys, session/condition binding, strictly increasing nonce,
candidate containment/hash, credential expiry, replay rejection, cross-session
rejection, development/regression seed admission, and held-out rejection.

The client must contain no adapter, rollout, dora, Genesis, budget, or ledger
implementation. It sends one request and prints one response.

- [ ] **Step 2: Add failing sandboxed-run construction tests**

Assert controller `run`:

- starts the broker from the trusted checkout;
- creates socket/credential files in a controller-owned `0700` runtime dir;
- records broker, client, sandbox-policy, agent-environment, Bubblewrap,
  adapter, runner, and frozen-source hashes;
- replaces direct agent execution with Bubblewrap argv;
- does not expose the trusted controller path or raw capability;
- mounts only the read-only client and socket directory;
- passes the attempt-client instruction in the prompt;
- rejects startup when the sandbox probe is not exactly valid.

- [ ] **Step 3: Verify RED**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH="$PWD/src:$PWD" \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  -k 'attempt_client or broker_auth or sandboxed_run' -q
```

Expected: missing client and sandbox integration assertions fail.

- [ ] **Step 4: Implement immutable JSON attempt client**

The client reads the socket path and short-lived credential from its
read-only mounted runtime configuration, hashes the candidate inside the
sandbox, and sends one bounded request. It rejects absolute candidate paths,
symlink escape, malformed seeds, unknown response keys, multiple JSON objects,
and responses larger than 1 MiB.

- [ ] **Step 5: Harden the trusted broker**

Keep adapter execution in the already-running trusted controller process.
Bind credentials to the session, condition, socket inode, controller hashes,
start/expiry, and monotonic nonce. The broker independently resolves and
hashes the trusted session candidate; it never trusts the client hash alone.

Under one exclusive attempt lock:

1. reload session state;
2. authenticate and validate request;
3. validate seed domain;
4. reserve episode capacity;
5. append admission and fsync;
6. invoke the assigned trusted adapter;
7. append canonical attempt and manifest;
8. append settlement and fsync.

Unsettled admissions conservatively charge requested episodes.

- [ ] **Step 6: Run the agent only through Bubblewrap**

Replace the direct `agent_cmd_campaign` process boundary in this controller
with:

```text
bwrap <verified-policy> -- <claude-or-codex-command>
```

The vendor command may retain its internal permission flags because the outer
namespace is the actual security boundary. The controller kills the entire
Bubblewrap process group on token, wall, telemetry, broker, or policy failure.
Require finite `wall_h`. Missing vendor usage fields, an over-ceiling event,
stdout EOF without process exit, or sandbox probe drift are terminal failures.

- [ ] **Step 7: Pin script and AISLE execution to trusted session source**

Ensure both adapter conditions receive the pinned session root explicitly.
For script execution, wrapper path, run directory, `PYTHONPATH`, budget ledger,
preflight worker, and raw evidence must all resolve beneath the trusted session
root. Candidate-controlled paths never enter trusted `PYTHONPATH`.

- [ ] **Step 8: Lock held-out scoring admission**

Use a stable session lock file. After acquiring it, reload state and atomically
write a terminal `scoring_started` admission with scorer nonce, controller
hashes, candidate hash, held-out seed hash, and start time before rollout. A
crash or adapter error writes or retains terminal invalid state and cannot be
retried.

- [ ] **Step 9: Extend audit correlation**

Audit:

- exact sandbox policy/probe/environment/client/broker hashes;
- monotonically authenticated broker requests;
- one-to-one admission/attempt/manifest/settlement;
- conservative requested charge for unsettled attempts;
- total charged development episodes at or below session ceiling;
- no held-out request before scoring admission;
- scorer nonce and manifest hash attribution;
- no surviving sandbox/broker/dora/Genesis process;
- no unowned run manifest.

Do not claim process-name monitoring alone proves confinement; namespace probe
evidence is mandatory.

- [ ] **Step 10: Verify GREEN and commit**

Run:

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_PROJECT_ENVIRONMENT=/home/demo/Public/github_aisle/aisle-latest/.venv \
  PYTHONPATH="$PWD/src:$PWD" \
  uv run --no-sync pytest tests/unit/test_s1_harness_ablation.py \
  tests/unit/test_campaign.py -q
```

Then run full unit, Ruff, trace check, and `git diff --check`.

Commit:

```bash
git add tools/s1_harness_attempt_client.py tools/s1_harness_ablation.py \
  src/aisle/harness/ablation_adapters.py \
  src/aisle/harness/script_preflight.py \
  src/aisle/harness/script_preflight_worker.py \
  src/aisle/harness/script_rollout.py \
  tests/unit/test_s1_harness_ablation.py
git commit -m "feat: enforce trusted S1 attempt brokerage"
```

---

### Task 4: Native isolation acceptance and bounded brokered CUDA attempt

**Files:**
- Create: `tests/accept/test_s1_native_isolation.py`
- Create: `docs/s1-harness-ablation-operator-guide.md`
- Modify: `tools/s1_harness_ablation.py`

**Interfaces:**
- CLI:
  `python tools/s1_harness_ablation.py sandbox-check --session <id>`
  (internal diagnostic; public help continues to list only
  `prepare`, `run`, `score`, `audit`)
- Produces: `sandbox-evidence.json`
- Consumed by: Task 7 audit and later protocol lock

- [ ] **Step 1: Add failing zero-cost native acceptance tests**

Start Bubblewrap with a harmless probe, not a model agent. Assert:

- worktree write succeeds;
- DNS resolution succeeds;
- host PID is invisible;
- NVIDIA devices are absent;
- container sockets are absent;
- unrelated worktrees are absent;
- Genesis import fails;
- dora executable lookup fails;
- attempt socket exists;
- copied/replayed/cross-session credentials fail;
- authenticated current-session request reaches a fake adapter;
- sandbox process-tree cleanup leaves no child.

Mark the test `accept`, not `unit`.

- [ ] **Step 2: Verify RED**

Run the acceptance file. Expected: missing sandbox-check/evidence integration.

- [ ] **Step 3: Implement evidence capture and audit binding**

Write `sandbox-evidence.json` atomically with:

```json
{
  "ok": true,
  "bwrap_sha256": "64 hex",
  "policy_sha256": "64 hex",
  "agent_env_sha256": "64 hex",
  "client_sha256": "64 hex",
  "probe": {},
  "started_at_epoch": 0.0,
  "ended_at_epoch": 0.0
}
```

Audit requires the file, verifies all hashes against prepared state, and
rejects unknown/missing probe fields or any prohibited visibility.

- [ ] **Step 4: Add one bounded brokered CUDA acceptance**

With no research agent, start the real broker and a harmless sandbox client
for one development seed. The client requests the attempt; the trusted host
adapter runs the already-installed CUDA environment.

Assert:

- sandbox probe reports no GPU;
- host trace reports the configured CUDA backend;
- admission precedes adapter launch;
- canonical attempt, safety evidence, manifest, and settlement exist;
- wrapper/runner hashes match pinned trusted source;
- no task-owned Bubblewrap, broker, dora, Genesis, or runtime process remains.

Use one simulator workload, a bounded timeout, and the brief's documented
task-failure branch if the incomplete starter does not reach the frozen
verifier deadline. Do not sync or reinstall anything.

- [ ] **Step 5: Document native operation**

Add operator commands for:

- building/verifying the agent environment;
- running the sandbox check;
- diagnosing refused namespace/device/socket probes;
- cleaning a contaminated session;
- verifying host CUDA remains unchanged;
- starting a later pilot only after separate authorization.

- [ ] **Step 6: Run complete gates and commit**

Run:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="$PWD/src:$PWD" uv run --no-sync pytest -m unit
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="$PWD/src:$PWD" uv run --no-sync pytest \
  tests/accept/test_s1_native_isolation.py -q
uv run --no-sync python tools/trace_check.py --root .
uv run --no-sync python tools/env_hash.py --check
```

Commit:

```bash
git add tests/accept/test_s1_native_isolation.py \
  docs/s1-harness-ablation-operator-guide.md tools/s1_harness_ablation.py
git commit -m "test: accept native S1 agent isolation"
```

Stop after acceptance. Do not start a paid research agent, pilot, main study,
or held-out campaign.
