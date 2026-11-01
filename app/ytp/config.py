"""Configuration, read once from the environment at startup (issue #8).

Everything the app needs comes from environment variables, so the same image runs in
Compose, in a test and on a laptop with nothing but `env` changing. The names are the ones
in `.env.example`; docker-compose.yml passes the app the ones it needs.

Standard library only: a small frozen dataclass is enough, and it keeps the image small.
All problems are collected and reported together, so a bad .env is fixed in one go.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

# Media type -> the environment variable naming its library folder (relative to DATA_ROOT).
# Same variables and meaning as .env.example and scripts/host/create-layout.sh.
LIBRARY_VARS = {
    "movies": "MEDIA_DIR_MOVIES",
    "series": "MEDIA_DIR_SERIES",
    "audiobooks": "MEDIA_DIR_AUDIOBOOKS",
    "music": "MEDIA_DIR_MUSIC",
    "youtube": "MEDIA_DIR_YOUTUBE",
}


class ConfigError(Exception):
    """The environment is not usable. `problems` lists every reason, one per line."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    host: str = "0.0.0.0"  # inside the container; compose publishes no port (see docs)
    port: int = 8000
    data_root: Path = Path("/data")
    config_dir: Path = Path("/config")
    # media type -> folder name relative to data_root, e.g. {"audiobooks": "Hörbücher"}
    library_dirs: Mapping[str, str] = field(default_factory=dict)
    jellyfin_url: str = ""
    # A secret: never shown in repr/str, never logged (see logging_setup.redact).
    jellyfin_api_key: str = field(default="", repr=False)

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @property
    def downloads_dir(self) -> Path:
        return self.data_root / "downloads"

    def library_path(self, media_type: str) -> Path:
        return self.data_root / self.library_dirs[media_type]

    def summary(self) -> dict[str, object]:
        """A log-safe description of the configuration (no secrets)."""
        return {
            "log_level": self.log_level,
            "data_root": str(self.data_root),
            "libraries": dict(self.library_dirs),
            "jellyfin_url": self.jellyfin_url or None,
            "jellyfin_configured": self.jellyfin_configured,
        }

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        problems: list[str] = []

        def text(name: str, default: str = "") -> str:
            """The trimmed value; unset and empty both mean "use the default"."""
            return env.get(name, "").strip() or default

        log_level = text("LOG_LEVEL", "INFO").upper() or "INFO"
        if log_level not in LOG_LEVELS:
            problems.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

        port = 8000
        raw_port = text("APP_PORT", "8000")
        if raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            port = int(raw_port)
        else:
            problems.append(f"APP_PORT must be a port number (1-65535), got {raw_port!r}")

        data_root = Path(text("DATA_ROOT", "/data"))
        if not data_root.is_absolute():
            problems.append(f"DATA_ROOT must be an absolute path, got {str(data_root)!r}")
        config_dir = Path(text("CONFIG_DIR", "/config"))
        if not config_dir.is_absolute():
            problems.append(f"CONFIG_DIR must be an absolute path, got {str(config_dir)!r}")

        library_dirs: dict[str, str] = {}
        for media_type, var in LIBRARY_VARS.items():
            name = text(var)
            if not name:
                problems.append(f"{var} must be set (library folder for {media_type})")
            elif not valid_folder_name(name):
                problems.append(
                    f"{var}={name!r} must be a single folder name relative to DATA_ROOT"
                )
            else:
                library_dirs[media_type] = name
        if len(set(library_dirs.values())) != len(library_dirs):
            problems.append("every library needs its own folder: two MEDIA_DIR_* values are equal")

        jellyfin_url = text("JELLYFIN_URL").rstrip("/")
        if jellyfin_url:
            parsed = urlparse(jellyfin_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                problems.append(f"JELLYFIN_URL must be an http(s) address, got {jellyfin_url!r}")

        if problems:
            raise ConfigError(problems)
        return cls(
            log_level=log_level,
            host=text("APP_HOST", "0.0.0.0"),
            port=port,
            data_root=data_root,
            config_dir=config_dir,
            library_dirs=library_dirs,
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=text("JELLYFIN_API_KEY"),
        )


def valid_folder_name(name: str) -> bool:
    """One path component: nothing that could leave DATA_ROOT or be read as an option."""
    return (
        name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and not name.startswith("-")
        and name.isprintable()
    )


def log_level_number(level: str) -> int:
    return int(getattr(logging, level))
