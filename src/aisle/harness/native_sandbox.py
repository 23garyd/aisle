"""Pure Bubblewrap policy construction for isolated S1 research agents.

This module does not start Bubblewrap, an agent, or a simulator.  It validates
an explicit mount policy, returns an argv list, and defines the harmless probe
contract that a trusted controller can execute later.
"""

from __future__ import annotations

import os
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path

BWRAP = Path("/usr/bin/bwrap")

FORBIDDEN_SOURCES = (
    Path("/dev/nvidia0"),
    Path("/dev/nvidiactl"),
    Path("/var/run/docker.sock"),
    Path("/run/docker.sock"),
    Path("/run/containerd/containerd.sock"),
)
_FORBIDDEN_PSEUDO_FILESYSTEMS = (Path("/dev"), Path("/proc"), Path("/sys"))

_CONTROLLER_ROOT = Path(__file__).resolve().parents[3]
_WORKTREES_ROOT = (
    _CONTROLLER_ROOT.parent
    if _CONTROLLER_ROOT.parent.name == ".worktrees"
    else _CONTROLLER_ROOT / ".worktrees"
)
_PROJECT_ROOT = _WORKTREES_ROOT.parent if _WORKTREES_ROOT.name == ".worktrees" else _CONTROLLER_ROOT
_HOST_SIMULATION_ENV = _PROJECT_ROOT / ".venv"
_PROTECTED_SOURCES = (_CONTROLLER_ROOT, _WORKTREES_ROOT, _HOST_SIMULATION_ENV)

# Destinations are deliberately explicit.  In particular, neither the host
# root nor /usr/bin is mounted into the sandbox.
_SYSTEM_READONLY_MOUNTS = (
    (Path("/usr/lib"), Path("/usr/lib")),
    (Path("/usr/lib64"), Path("/usr/lib64")),
    (Path("/lib"), Path("/lib")),
    (Path("/lib64"), Path("/lib64")),
    (Path("/etc/ssl/certs"), Path("/etc/ssl/certs")),
    (Path("/etc/ca-certificates"), Path("/etc/ca-certificates")),
    (Path("/usr/share/ca-certificates"), Path("/usr/share/ca-certificates")),
    (Path("/etc/resolv.conf"), Path("/etc/resolv.conf")),
    (Path("/etc/hosts"), Path("/etc/hosts")),
    (Path("/etc/nsswitch.conf"), Path("/etc/nsswitch.conf")),
    (Path("/etc/gai.conf"), Path("/etc/gai.conf")),
)

_COMMON_ENV = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_VENDOR_ENV = {
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
    ),
    "codex": frozenset(
        {
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_ORGANIZATION",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
            "OPENAI_PROJECT_ID",
        }
    ),
}

_PROBE_EXPECTED = (
    ("worktree_write", True),
    ("network_dns", True),
    ("host_process_visible", False),
    ("nvidia_visible", False),
    ("docker_socket_visible", False),
    ("genesis_importable", False),
    ("dora_executable", False),
    ("other_worktree_visible", False),
    ("attempt_socket_visible", True),
)

_PROBE_SOURCE_TEMPLATE = textwrap.dedent(
    """\
    import importlib.util
    import json
    import os
    import socket
    import stat
    import sys
    import tempfile
    import zipfile
    from pathlib import Path

    IMPORT_SEARCH_LOCATIONS = tuple(sys.path)
    HOST_PID_NAMESPACE = __HOST_PID_NAMESPACE__

    def check(operation):
        try:
            return bool(operation())
        except Exception:
            return None

    def worktree_write():
        with tempfile.NamedTemporaryFile(dir="/workspace", prefix=".aisle-probe-"):
            return True

    def network_dns():
        socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
        return True

    def host_process_visible():
        return os.stat("/proc/self/ns/pid").st_ino == HOST_PID_NAMESPACE

    def paths_visible(paths):
        for path in paths:
            try:
                path.stat()
            except FileNotFoundError:
                continue
            return True
        return False

    def nvidia_visible():
        return any(entry.name.startswith("nvidia") for entry in Path("/dev").iterdir())

    def executable_visible(name):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            candidate = Path(directory) / name
            try:
                mode = candidate.stat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode) and mode & 0o111:
                return True
        return False

    def validate_import_search_locations():
        for raw_location in IMPORT_SEARCH_LOCATIONS:
            if type(raw_location) is not str or not raw_location:
                raise ValueError("malformed import search location")
            location = Path(raw_location).expanduser().resolve(strict=False)
            try:
                mode = location.stat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                with os.scandir(location) as entries:
                    next(entries, None)
            elif stat.S_ISREG(mode):
                with zipfile.ZipFile(location) as archive:
                    archive.infolist()
            else:
                raise OSError("unsupported import search location")

    def genesis_importable():
        validate_import_search_locations()
        return importlib.util.find_spec("genesis") is not None

    result = {
        "worktree_write": check(worktree_write),
        "network_dns": check(network_dns),
        "host_process_visible": check(host_process_visible),
        "nvidia_visible": check(nvidia_visible),
        "docker_socket_visible": check(
            lambda: paths_visible(
                (
                    Path("/var/run/docker.sock"),
                    Path("/run/docker.sock"),
                    Path("/run/containerd/containerd.sock"),
                )
            )
        ),
        "genesis_importable": check(genesis_importable),
        "dora_executable": check(lambda: executable_visible("dora")),
        "other_worktree_visible": check(
            lambda: paths_visible(
                (
                    Path("/.worktrees"),
                    Path("/repo/.worktrees"),
                    Path("/worktrees"),
                    Path("/workspace/../.worktrees"),
                )
            )
        ),
        "attempt_socket_visible": check(lambda: Path("/run/aisle/attempt.sock").is_socket()),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    """
)


@dataclass(frozen=True)
class SandboxPolicy:
    """Host paths approved for one isolated research-agent process."""

    worktree: Path
    agent_env: Path
    runtime_dir: Path
    attempt_client: Path
    agent_executable: Path
    credential_mounts: tuple[Path, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "worktree",
            "agent_env",
            "runtime_dir",
            "attempt_client",
            "agent_executable",
        ):
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be a Path")
        if not isinstance(self.credential_mounts, tuple) or not all(
            isinstance(path, Path) for path in self.credential_mounts
        ):
            raise TypeError("credential_mounts must be a tuple of Path values")


def _contains(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _overlaps(path: Path, protected: Path) -> bool:
    return _contains(path, protected) or _contains(protected, path)


def _resolve_source(path: Path, label: str, *, allow_protected_descendant: bool = False) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    forbidden = tuple(
        source.resolve(strict=False)
        for source in (*FORBIDDEN_SOURCES, *_FORBIDDEN_PSEUDO_FILESYSTEMS)
    )
    if any(_overlaps(candidate, source) for source in forbidden):
        raise ValueError(f"{label} is a forbidden sandbox bind source: {candidate}")
    protected = tuple(source.resolve(strict=False) for source in _PROTECTED_SOURCES)
    overlaps_protected = any(_overlaps(candidate, source) for source in protected)
    if allow_protected_descendant:
        worktrees_root = _WORKTREES_ROOT.resolve(strict=False)
        controller_root = _CONTROLLER_ROOT.resolve(strict=False)
        if candidate.parent != worktrees_root or candidate == controller_root:
            raise ValueError(
                "worktree must be a direct child of the configured worktrees root "
                "and distinct from the controller"
            )
        contains_protected = any(_contains(source, candidate) for source in protected)
        overlaps_protected = contains_protected
    if overlaps_protected:
        raise ValueError(f"{label} overlaps a protected host source: {candidate}")
    if not candidate.exists():
        raise ValueError(f"{label} does not exist: {candidate}")
    return candidate.resolve(strict=True)


def _validate_outside_worktree(source: Path, worktree: Path, label: str) -> None:
    if _overlaps(source, worktree):
        raise ValueError(f"{label} must be outside and not contain the writable worktree")


def _selected_vendor(executable: Path) -> str:
    name = executable.name.lower()
    vendors = [vendor for vendor in _VENDOR_ENV if vendor in name]
    if len(vendors) != 1:
        raise ValueError("agent_executable name must select exactly one of claude or codex")
    return vendors[0]


def _validated_policy_sources(
    policy: SandboxPolicy,
) -> tuple[Path, Path, Path, Path, Path, tuple[Path, ...]]:
    worktree = _resolve_source(policy.worktree, "worktree", allow_protected_descendant=True)
    agent_env = _resolve_source(policy.agent_env, "agent_env")
    runtime_dir = _resolve_source(policy.runtime_dir, "runtime_dir")
    attempt_client = _resolve_source(policy.attempt_client, "attempt_client")
    agent_executable = _resolve_source(policy.agent_executable, "agent_executable")
    credentials = tuple(
        _resolve_source(path, f"credential_mounts[{index}]")
        for index, path in enumerate(policy.credential_mounts)
    )

    if not worktree.is_dir():
        raise ValueError("worktree must be a directory")
    if not agent_env.is_dir():
        raise ValueError("agent_env must be a directory")
    if not runtime_dir.is_dir():
        raise ValueError("runtime_dir must be a directory")
    if not attempt_client.is_file():
        raise ValueError("attempt_client must be a file")
    if not agent_executable.is_file():
        raise ValueError("agent_executable must be a file")
    if any(not source.is_file() and not source.is_dir() for source in credentials):
        raise ValueError("credential mounts must be regular files or directories")

    runtime_stat = runtime_dir.stat()
    if runtime_stat.st_uid != os.getuid():
        raise ValueError("runtime_dir must be owned by the current uid")
    if stat.S_IMODE(runtime_stat.st_mode) != 0o700:
        raise ValueError("runtime_dir mode must be exactly 0700")

    for source, label in (
        (agent_env, "agent_env"),
        (runtime_dir, "attempt socket runtime_dir"),
        (attempt_client, "attempt_client"),
        (agent_executable, "agent_executable"),
        *((source, f"credential_mounts[{index}]") for index, source in enumerate(credentials)),
    ):
        _validate_outside_worktree(source, worktree, label)

    credential_names = [source.name for source in credentials]
    if len(set(credential_names)) != len(credential_names):
        raise ValueError("credential mount basenames must be unique")

    return worktree, agent_env, runtime_dir, attempt_client, agent_executable, credentials


def _append_readonly(argv: list[str], source: Path, destination: str) -> None:
    argv.extend(("--ro-bind", str(source), destination))


def build_bwrap_argv(policy: SandboxPolicy, command: list[str], env: dict[str, str]) -> list[str]:
    """Validate *policy* and return a shell-free Bubblewrap argv.

    Unknown environment variables are ignored.  This permits the trusted
    caller to pass a host environment without granting arbitrary inheritance.
    """

    if not isinstance(policy, SandboxPolicy):
        raise TypeError("policy must be a SandboxPolicy")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) and value and "\0" not in value for value in command)
    ):
        raise ValueError("command must be a non-empty list of non-empty strings")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise TypeError("env must be a dict of string keys and values")

    (
        worktree,
        agent_env,
        runtime_dir,
        attempt_client,
        agent_executable,
        credentials,
    ) = _validated_policy_sources(policy)
    vendor = _selected_vendor(policy.agent_executable)

    argv = [
        str(BWRAP),
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--clearenv",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/agent-home",
        "--bind",
        str(worktree),
        "/workspace",
    ]
    _append_readonly(argv, agent_env, "/agent-env")
    _append_readonly(argv, runtime_dir, "/run/aisle")
    _append_readonly(argv, attempt_client, "/opt/aisle/attempt-client")
    _append_readonly(argv, agent_executable, "/opt/aisle/agent")
    for source in credentials:
        _append_readonly(argv, source, f"/agent-home/{source.name}")

    for configured_source, destination in _SYSTEM_READONLY_MOUNTS:
        if configured_source.exists():
            source = _resolve_source(configured_source, f"system mount {destination}")
            _append_readonly(argv, source, str(destination))

    fixed_env = {
        "AISLE_ABLATION_CLIENT": "/opt/aisle/attempt-client",
        "AISLE_ABLATION_SOCKET": "/run/aisle/attempt.sock",
        "HOME": "/agent-home",
        "PATH": "/agent-env/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }
    allowed_env = _COMMON_ENV | _VENDOR_ENV[vendor]
    selected_env = {
        key: value for key, value in env.items() if key in allowed_env and "\0" not in value
    }
    for key, value in sorted((fixed_env | selected_env).items()):
        argv.extend(("--setenv", key, value))

    argv.extend(("--chdir", "/workspace", "--", *command))
    return argv


def verify_sandbox_probe(result: dict) -> tuple[bool, tuple[str, ...]]:
    """Require the exact probe schema, boolean types, and safe values."""

    if not isinstance(result, dict):
        return False, ("result:not_object",)

    expected = dict(_PROBE_EXPECTED)
    unknown = sorted(
        result.keys() - expected.keys(), key=lambda key: (type(key).__name__, repr(key))
    )
    errors = [
        *(f"missing:{key}" for key, _ in _PROBE_EXPECTED if key not in result),
        *(f"unknown:{key}" for key in unknown),
    ]
    for key, wanted in _PROBE_EXPECTED:
        if key not in result:
            continue
        actual = result[key]
        if type(actual) is not bool or actual is not wanted:
            errors.append(f"value:{key}:expected={wanted!r}:actual={actual!r}")
    return not errors, tuple(errors)


def sandbox_probe_command() -> list[str]:
    """Return the harmless, JSON-only probe command for the agent environment."""

    host_pid_namespace = os.stat("/proc/self/ns/pid").st_ino
    source = _PROBE_SOURCE_TEMPLATE.replace("__HOST_PID_NAMESPACE__", str(host_pid_namespace))
    return ["/agent-env/bin/python", "-I", "-c", source]
