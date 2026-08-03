#!/usr/bin/env python3
"""Build and inspect the controller-owned non-simulation S1 agent environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_MANIFEST_NAME = ".aisle-s1-agent-environment.json"
_DISTRIBUTION_INVENTORY_SOURCE = """\
import importlib.metadata
import json

items = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    version = distribution.version
    items.append(f"{name}=={version}")
print(json.dumps(sorted(items)))
"""
_FORBIDDEN_DISTRIBUTIONS = frozenset({"genesis-world", "dora-rs", "torch"})
_FORBIDDEN_EXECUTABLES = frozenset({"dora", "genesis"})
_AGENT_ENV_ROOT_NAME = ".s1-agent-environments"
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHARED_WRITE_MASK = stat.S_IWGRP | stat.S_IWOTH


@dataclass(frozen=True)
class AgentEnvironmentSpec:
    """Canonical identity and launch-surface inventory for one agent environment."""

    python: str
    lock_sha256: str
    distributions: tuple[str, ...]
    executables: tuple[str, ...]


class AgentEnvironmentError(ValueError):
    """Stable fail-closed environment inspection or construction error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_distribution_inventory(python: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", _DISTRIBUTION_INVENTORY_SOURCE],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", f"could not execute destination Python: {error}"
        ) from error
    if completed.returncode != 0:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "destination Python metadata inspection failed"
        )
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution inventory is not valid JSON"
        ) from error
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution inventory must be a list of strings"
        )
    return tuple(value)


def _normalized_distribution(item: str) -> str:
    name, separator, version = item.partition("==")
    if (
        separator != "=="
        or not name
        or not version
        or "==" in version
        or "\0" in item
        or any(character in item for character in "\r\n")
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name) is None
    ):
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution inventory entry is malformed"
        )
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "PEP 440 version validation is unavailable"
        ) from error
    try:
        Version(version)
    except InvalidVersion as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution version is not valid PEP 440"
        ) from error
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if not normalized:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution inventory name is malformed"
        )
    return normalized


def _executable_inventory(bin_dir: Path) -> tuple[str, ...]:
    try:
        entries = tuple(bin_dir.iterdir())
    except OSError as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", f"could not inspect destination bin directory: {error}"
        ) from error

    executables = []
    for entry in entries:
        try:
            mode = entry.stat().st_mode
        except OSError as error:
            raise AgentEnvironmentError(
                "INVENTORY_MALFORMED", f"could not inspect executable candidate: {error}"
            ) from error
        if stat.S_ISREG(mode) and mode & 0o111:
            executables.append(entry.name)
    return tuple(sorted(executables))


def _attested_lock_sha256(destination: Path) -> str:
    manifest = destination / _MANIFEST_NAME
    try:
        value = json.loads(manifest.read_bytes())
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "environment attestation is malformed"
        ) from error
    lock_sha256 = value.get("lock_sha256") if isinstance(value, dict) else None
    if not isinstance(lock_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", lock_sha256) is None:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "environment lock attestation is malformed"
        )
    return lock_sha256


def inspect_agent_environment(destination: Path) -> AgentEnvironmentSpec:
    """Inspect an environment without importing any installed project package."""

    destination = destination.resolve(strict=False)
    python = destination / "bin" / "python"
    try:
        python_mode = python.stat().st_mode
    except OSError as error:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", f"destination Python is unavailable: {error}"
        ) from error
    if not stat.S_ISREG(python_mode) or not python_mode & 0o111:
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "destination Python must be an executable regular file"
        )

    distributions = tuple(sorted(_read_distribution_inventory(python)))
    normalized_names = [_normalized_distribution(item) for item in distributions]
    if len(set(normalized_names)) != len(normalized_names):
        raise AgentEnvironmentError(
            "INVENTORY_MALFORMED", "distribution inventory contains duplicate names"
        )
    forbidden_distribution = next(
        (
            name
            for name in normalized_names
            if name in _FORBIDDEN_DISTRIBUTIONS or name.startswith("nvidia-")
        ),
        None,
    )
    if forbidden_distribution is not None:
        raise AgentEnvironmentError(
            "FORBIDDEN_DISTRIBUTION",
            f"forbidden distribution present: {forbidden_distribution}",
        )

    executables = _executable_inventory(destination / "bin")
    forbidden_executable = next(
        (name for name in executables if name.casefold() in _FORBIDDEN_EXECUTABLES),
        None,
    )
    if forbidden_executable is not None:
        raise AgentEnvironmentError(
            "FORBIDDEN_EXECUTABLE",
            f"forbidden executable present: {forbidden_executable}",
        )

    return AgentEnvironmentSpec(
        python=str(python.resolve()),
        lock_sha256=_attested_lock_sha256(destination),
        distributions=distributions,
        executables=executables,
    )


def _provenance(destination: Path, spec: AgentEnvironmentSpec, lock_sha256: str) -> dict:
    builder_sha256 = _sha256_file(Path(__file__))
    python_sha256 = _sha256_file(Path(spec.python))
    distribution_inventory_sha256 = _sha256_json(list(spec.distributions))
    executable_inventory_sha256 = _sha256_json(list(spec.executables))
    identity = {
        "environment": str(destination),
        "builder_sha256": builder_sha256,
        "python_sha256": python_sha256,
        "distribution_inventory_sha256": distribution_inventory_sha256,
        "executable_inventory_sha256": executable_inventory_sha256,
        "python": spec.python,
        "lock_sha256": lock_sha256,
        "distributions": list(spec.distributions),
        "executables": list(spec.executables),
    }
    return {**identity, "inventory_sha256": _sha256_json(identity)}


def _secure_directory(
    path: Path,
    expected_parent: Path,
    *,
    create: bool,
    code: str,
) -> bool:
    if path.parent != expected_parent or path.resolve(strict=False) != path:
        raise AgentEnvironmentError(code, "directory is outside its canonical trusted parent")
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=False)
            path_stat = path.lstat()
        except OSError as error:
            raise AgentEnvironmentError(code, "could not create private directory") from error
    except OSError as error:
        raise AgentEnvironmentError(code, "could not inspect private directory") from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or stat.S_IMODE(path_stat.st_mode) & _SHARED_WRITE_MASK
    ):
        raise AgentEnvironmentError(
            code, "directory must be canonical, controller-owned, and not shared-writable"
        )
    return True


def _validate_build_paths(repo: Path, destination: Path) -> tuple[Path, Path, Path]:
    if not repo.is_absolute() or not destination.is_absolute():
        raise AgentEnvironmentError(
            "INVALID_ARGUMENT", "repo and destination must be explicit absolute paths"
        )
    requested_repo = repo
    requested_destination = destination
    repo = repo.resolve(strict=True)
    destination = requested_destination.resolve(strict=False)
    if (
        requested_repo != repo
        or not repo.is_dir()
        or repo.parent.name != ".worktrees"
        or repo.parent.parent == repo.parent
    ):
        raise AgentEnvironmentError(
            "INVALID_ARGUMENT",
            "repo must be one canonical direct child of controller .worktrees",
        )
    controller_root = repo.parent.parent
    worktrees_root = controller_root / ".worktrees"
    agent_environment_root = controller_root / _AGENT_ENV_ROOT_NAME
    if (
        requested_destination != destination
        or destination.parent != agent_environment_root
        or _ENVIRONMENT_NAME_PATTERN.fullmatch(destination.name) is None
        or destination == controller_root / ".venv"
        or destination == repo / ".venv"
        or destination.is_relative_to(worktrees_root)
    ):
        raise AgentEnvironmentError(
            "DESTINATION_NOT_ISOLATED",
            "destination must be one canonical child of the dedicated agent-environment root",
        )
    configured_host_environment = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if configured_host_environment:
        try:
            host_environment = Path(configured_host_environment).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            raise AgentEnvironmentError(
                "DESTINATION_NOT_ISOLATED", "host UV_PROJECT_ENVIRONMENT is malformed"
            ) from None
        if destination == host_environment:
            raise AgentEnvironmentError(
                "DESTINATION_NOT_ISOLATED",
                "destination cannot be the active host UV_PROJECT_ENVIRONMENT",
            )
    if (
        not controller_root.is_dir()
        or controller_root.is_symlink()
        or controller_root.stat().st_uid != os.getuid()
    ):
        raise AgentEnvironmentError(
            "DESTINATION_NOT_OWNED",
            "controller root must be canonical and controller-owned",
        )
    _secure_directory(
        agent_environment_root,
        controller_root,
        create=False,
        code="DESTINATION_NOT_OWNED",
    )
    lock = repo / "uv.lock"
    if not lock.is_file():
        raise AgentEnvironmentError("LOCK_MISSING", "repository uv.lock is unavailable")
    return repo, destination, lock


def _prepare_agent_environment_root(destination: Path) -> None:
    root = destination.parent
    _secure_directory(
        root,
        root.parent,
        create=True,
        code="DESTINATION_NOT_OWNED",
    )


def _write_manifest(destination: Path, provenance: dict) -> None:
    manifest = destination / _MANIFEST_NAME
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(manifest)


def _read_manifest(destination: Path) -> tuple[dict | None, bytes]:
    manifest = destination / _MANIFEST_NAME
    try:
        encoded = manifest.read_bytes()
    except FileNotFoundError:
        return None, b""
    except OSError:
        return None, b"unreadable-manifest"
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, encoded
    return (value if isinstance(value, dict) else None), encoded


def _quarantine_destination(destination: Path, evidence: bytes) -> str | None:
    agent_environment_root = destination.parent
    controller_root = agent_environment_root.parent
    quarantine_root = agent_environment_root / ".s1-agent-env-quarantine"
    tag_source = evidence or str(destination).encode()
    tag = hashlib.sha256(tag_source).hexdigest()[:16]
    target = quarantine_root / f"{destination.name}-{tag}"
    try:
        _secure_directory(
            agent_environment_root,
            controller_root,
            create=False,
            code="QUARANTINE_FAILED",
        )
        _secure_directory(
            quarantine_root,
            agent_environment_root,
            create=True,
            code="QUARANTINE_FAILED",
        )
    except AgentEnvironmentError:
        return "QUARANTINE_FAILED"
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return "QUARANTINE_FAILED"
    else:
        return "QUARANTINE_CONFLICT"
    try:
        destination.replace(target)
    except OSError:
        return "QUARANTINE_FAILED"
    return None


def _lock_matches(lock: Path, expected_sha256: str) -> bool:
    try:
        return _sha256_file(lock) == expected_sha256
    except OSError:
        return False


def _lock_drift_result(destination: Path, lock_sha256: str) -> dict:
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    else:
        _quarantine_destination(destination, f"lock-drift:{lock_sha256}".encode())
    return {"code": "LOCK_DRIFT", "ok": False}


def _failure_result(
    code: str,
    lock: Path,
    lock_sha256: str,
    destination: Path,
) -> dict:
    if not _lock_matches(lock, lock_sha256):
        return _lock_drift_result(destination, lock_sha256)
    return {"code": code, "ok": False}


def build_agent_environment(repo: Path, destination: Path, *, runner: Callable) -> dict:
    """Synchronize and attest one explicit non-simulation agent environment."""

    try:
        repo, destination, lock = _validate_build_paths(repo, destination)
        _prepare_agent_environment_root(destination)
    except (AgentEnvironmentError, OSError) as error:
        code = error.code if isinstance(error, AgentEnvironmentError) else "INVALID_ARGUMENT"
        return {"code": code, "ok": False}

    try:
        lock_sha256 = _sha256_file(lock)
    except OSError:
        return {"code": "LOCK_MISSING", "ok": False}

    if destination.exists():
        manifest, manifest_bytes = _read_manifest(destination)
        try:
            current_spec = inspect_agent_environment(destination)
            current_provenance = _provenance(destination, current_spec, lock_sha256)
        except (AgentEnvironmentError, OSError):
            current_provenance = None
        if not _lock_matches(lock, lock_sha256):
            return _lock_drift_result(destination, lock_sha256)
        if current_provenance is not None and manifest == current_provenance:
            return {"ok": True, "reused": True, **current_provenance}
        quarantine_error = _quarantine_destination(destination, manifest_bytes)
        if quarantine_error is not None:
            return _failure_result(quarantine_error, lock, lock_sha256, destination)

    if not _lock_matches(lock, lock_sha256):
        return _lock_drift_result(destination, lock_sha256)
    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(destination)
    command = ["uv", "sync", "--frozen", "--no-extra", "sim", "--group", "dev"]
    try:
        completed = runner(command, cwd=repo, env=environment)
    except Exception:  # noqa: BLE001 - normalize runner failures; never catch BaseException
        return _failure_result("SYNC_FAILED", lock, lock_sha256, destination)
    if not _lock_matches(lock, lock_sha256):
        return _lock_drift_result(destination, lock_sha256)
    if type(getattr(completed, "returncode", None)) is not int or completed.returncode != 0:
        return _failure_result("SYNC_FAILED", lock, lock_sha256, destination)

    try:
        inspected = inspect_agent_environment(destination)
    except AgentEnvironmentError as error:
        return _failure_result(error.code, lock, lock_sha256, destination)
    except OSError:
        return _failure_result("INVENTORY_MALFORMED", lock, lock_sha256, destination)
    if not _lock_matches(lock, lock_sha256):
        return _lock_drift_result(destination, lock_sha256)
    try:
        provenance = _provenance(destination, inspected, lock_sha256)
        _write_manifest(destination, provenance)
    except OSError:
        return _failure_result("INVENTORY_MALFORMED", lock, lock_sha256, destination)
    if not _lock_matches(lock, lock_sha256):
        return _lock_drift_result(destination, lock_sha256)

    return {"ok": True, "reused": False, **provenance}


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AgentEnvironmentError("ARGUMENT", message)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def _production_runner(command: list[str], *, cwd: Path, env: dict[str, str]):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the JSON-only agent-environment builder CLI."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument in {"-h", "--help"} for argument in arguments):
        result = {
            "ok": True,
            "usage": {"options": ["--repo <path>", "--out <path>"]},
        }
    else:
        try:
            args = _parser().parse_args(arguments)
            built = build_agent_environment(args.repo, args.out, runner=_production_runner)
            result = (
                {
                    "ok": True,
                    "environment": built["environment"],
                    "lock_sha256": built["lock_sha256"],
                    "inventory_sha256": built["inventory_sha256"],
                }
                if built.get("ok") is True
                else built
            )
        except AgentEnvironmentError as error:
            result = {"code": error.code, "ok": False}
        except Exception as error:  # noqa: BLE001 - preserve the JSON-only CLI contract
            print(f"[s1-agent-env] unexpected error: {error!r}", file=sys.stderr)
            result = {"code": "INTERNAL", "ok": False}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
