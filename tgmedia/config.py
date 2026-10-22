"""Credentials and the session file, environment first then the config file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ENV_API_ID = "TG_API_ID"
ENV_API_HASH = "TG_API_HASH"
ENV_CONFIG_DIR = "TGMEDIA_CONFIG_DIR"


class ConfigError(RuntimeError):
    """The config file exists but cannot be used."""


@dataclass(frozen=True, slots=True)
class Credentials:
    api_id: int
    api_hash: str


def config_dir() -> Path:
    override = os.environ.get(ENV_CONFIG_DIR)
    return Path(override).expanduser() if override else Path.home() / ".config" / "tgmedia"


def config_path() -> Path:
    return config_dir() / "config.toml"


def session_path() -> Path:
    return config_dir() / "session"


def read_credentials() -> Credentials | None:
    env_id = os.environ.get(ENV_API_ID)
    env_hash = os.environ.get(ENV_API_HASH)
    if env_id and env_hash:
        return _credentials(env_id, env_hash, f"{ENV_API_ID}/{ENV_API_HASH}")
    path = config_path()
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    return _credentials(data.get("api_id"), data.get("api_hash"), str(path))


def _credentials(raw_id: object, raw_hash: object, origin: str) -> Credentials:
    try:
        api_id = int(raw_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(f"api_id in {origin} is not a number") from None
    if not isinstance(raw_hash, str) or not raw_hash.strip():
        raise ConfigError(f"api_hash in {origin} is missing")
    return Credentials(api_id=api_id, api_hash=raw_hash.strip())


def write_credentials(credentials: Credentials) -> Path:
    path = config_path()
    _write_private(path, f'api_id = {credentials.api_id}\napi_hash = "{credentials.api_hash}"\n')
    return path


def read_session() -> str:
    try:
        return session_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_session(session: str) -> Path:
    path = session_path()
    _write_private(path, session)
    return path


def _write_private(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    # An existing file keeps its old mode through O_CREAT, so pin it again.
    os.chmod(path, 0o600)
