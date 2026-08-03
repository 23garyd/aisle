#!/usr/bin/env python3
"""Send one authenticated S1 candidate request through the mounted Unix socket."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import sys
import time
from pathlib import Path

_MAX_MESSAGE_BYTES = 1024 * 1024
_CONFIG_KEYS = frozenset(
    {
        "protocol",
        "session_id",
        "condition",
        "credential",
        "candidate_relpath",
        "expires_at_epoch",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "protocol",
        "session_id",
        "condition",
        "nonce",
        "credential",
        "candidate_relpath",
        "candidate_sha256",
        "seeds",
    }
)
_SUCCESS_KEYS = frozenset({"ok", "attempt_id", "episodes_used", "episodes_left"})
_ERROR_KEYS = frozenset({"ok", "code", "error"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEED = re.compile(r"0|[1-9][0-9]*")


class AttemptClientError(ValueError):
    """A stable local refusal that can be rendered as one JSON object."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AttemptClientError("ARGUMENT", message)


def _single_object(encoded: bytes, *, label: str) -> dict:
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise AttemptClientError("PROTOCOL", f"{label} exceeds the size limit")
    try:
        text = encoded.decode("utf-8")
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(text.lstrip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttemptClientError("PROTOCOL", f"{label} is not one JSON object") from exc
    if text.lstrip()[end:].strip() or not isinstance(value, dict):
        raise AttemptClientError("PROTOCOL", f"{label} is not one JSON object")
    return value


def _read_config(path: Path, now_epoch: float) -> dict:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise AttemptClientError("CONFIG", "mounted request configuration is unavailable") from exc
    value = _single_object(encoded, label="configuration")
    if set(value) != _CONFIG_KEYS:
        raise AttemptClientError("CONFIG", "configuration schema is invalid")
    expires = value["expires_at_epoch"]
    if (
        value["protocol"] != 1
        or isinstance(value["protocol"], bool)
        or not isinstance(value["session_id"], str)
        or not value["session_id"]
        or value["condition"] not in {"aisle", "script"}
        or not isinstance(value["credential"], str)
        or _SHA256.fullmatch(value["credential"]) is None
        or not isinstance(value["candidate_relpath"], str)
        or not value["candidate_relpath"]
        or isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not math.isfinite(float(expires))
    ):
        raise AttemptClientError("CONFIG", "configuration values are invalid")
    if now_epoch >= float(expires):
        raise AttemptClientError("EXPIRED", "mounted request configuration expired")
    return value


def _seed_csv(spec: str) -> tuple[int, ...]:
    if not isinstance(spec, str) or not spec:
        raise AttemptClientError("SEEDS", "seeds must be a non-empty CSV")
    pieces = spec.split(",")
    if any(_SEED.fullmatch(piece) is None for piece in pieces):
        raise AttemptClientError("SEEDS", "seeds must be decimal CSV values")
    values = tuple(int(piece) for piece in pieces)
    if len(set(values)) != len(values):
        raise AttemptClientError("SEEDS", "seeds must be unique")
    return values


def _candidate(workspace: Path, relative: str) -> Path:
    candidate_relative = Path(relative)
    if (
        candidate_relative.is_absolute()
        or not candidate_relative.parts
        or any(part in {"", ".", ".."} for part in candidate_relative.parts)
        or "\0" in relative
    ):
        raise AttemptClientError("CANDIDATE", "candidate path must be a contained relative path")
    try:
        boundary = workspace.resolve(strict=True)
        candidate = (boundary / candidate_relative).resolve(strict=True)
    except OSError as exc:
        raise AttemptClientError("CANDIDATE", "candidate is unavailable") from exc
    if not boundary.is_dir() or not candidate.is_relative_to(boundary) or not candidate.is_file():
        raise AttemptClientError("CANDIDATE", "candidate escaped the assigned workspace")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AttemptClientError("CANDIDATE", "candidate could not be hashed") from exc
    return digest.hexdigest()


def build_request(
    seeds: str,
    *,
    config_path: Path,
    workspace: Path,
    nonce: int,
    now_epoch: float,
) -> dict:
    """Construct the exact request after locally validating mounted inputs."""

    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= 0:
        raise AttemptClientError("NONCE", "nonce must be a positive integer")
    _seed_csv(seeds)
    config = _read_config(config_path, now_epoch)
    candidate = _candidate(workspace, config["candidate_relpath"])
    request = {
        "protocol": 1,
        "session_id": config["session_id"],
        "condition": config["condition"],
        "nonce": nonce,
        "credential": config["credential"],
        "candidate_relpath": config["candidate_relpath"],
        "candidate_sha256": _sha256_file(candidate),
        "seeds": seeds,
    }
    assert set(request) == _REQUEST_KEYS
    return request


def parse_response(encoded: bytes) -> dict:
    """Accept only one bounded response with the exact success or error schema."""

    response = _single_object(encoded, label="response")
    if type(response.get("ok")) is not bool:
        raise AttemptClientError("PROTOCOL", "response status is invalid")
    expected = _SUCCESS_KEYS if response["ok"] else _ERROR_KEYS
    if set(response) != expected:
        raise AttemptClientError("PROTOCOL", "response schema is invalid")
    if response["ok"]:
        if (
            not isinstance(response["attempt_id"], str)
            or not response["attempt_id"]
            or isinstance(response["episodes_used"], bool)
            or not isinstance(response["episodes_used"], int)
            or response["episodes_used"] < 0
            or isinstance(response["episodes_left"], bool)
            or not isinstance(response["episodes_left"], int)
            or response["episodes_left"] < 0
        ):
            raise AttemptClientError("PROTOCOL", "response values are invalid")
    elif (
        not isinstance(response["code"], str)
        or not response["code"]
        or not isinstance(response["error"], str)
        or not response["error"]
    ):
        raise AttemptClientError("PROTOCOL", "response values are invalid")
    return response


def _exchange(socket_path: Path, request: dict, timeout_s: float) -> dict:
    encoded = (
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    received = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_s)
            client.connect(str(socket_path))
            client.sendall(encoded)
            client.shutdown(socket.SHUT_WR)
            while True:
                chunk = client.recv(min(65536, _MAX_MESSAGE_BYTES + 1 - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
                if len(received) > _MAX_MESSAGE_BYTES:
                    raise AttemptClientError("PROTOCOL", "response exceeds the size limit")
    except AttemptClientError:
        raise
    except OSError as exc:
        raise AttemptClientError("UNAVAILABLE", "request socket is unavailable") from exc
    return parse_response(bytes(received))


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
        now_epoch = time.time()
        request = build_request(
            args.seeds,
            config_path=Path(os.environ.get("AISLE_ABLATION_CONFIG", "/run/aisle/credential.json")),
            workspace=Path(os.environ.get("AISLE_ABLATION_WORKSPACE", "/workspace")),
            nonce=time.monotonic_ns(),
            now_epoch=now_epoch,
        )
        result = _exchange(
            Path(os.environ.get("AISLE_ABLATION_SOCKET", "/run/aisle/attempt.sock")),
            request,
            4 * 3600.0,
        )
    except AttemptClientError as exc:
        result = {"ok": False, "code": exc.code, "error": exc.detail}
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
