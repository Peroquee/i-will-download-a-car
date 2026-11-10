"""Configuration, read once from the environment at startup (issue #8).

Everything the app needs comes from environment variables, so the same image runs in
Compose, in a test and on a laptop with nothing but `env` changing. The names are the ones
in `.env.example`; docker-compose.yml passes the app the ones it needs.

Standard library only: a small frozen dataclass is enough, and it keeps the image small.
All problems are collected and reported together, so a bad .env is fixed in one go.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import accounts, passwords
from .egress import parse_networks

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

# Media type -> the environment variable naming its library folder (relative to DATA_ROOT).
# Same variables and meaning as .env.example and scripts/host/create-layout.sh.
LIBRARY_VARS = {
    "movies": "MEDIA_DIR_MOVIES",
    "series": "MEDIA_DIR_SERIES",
    "audiobooks": "MEDIA_DIR_AUDIOBOOKS",
    "music": "MEDIA_DIR_MUSIC",
    "podcasts": "MEDIA_DIR_PODCASTS",
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
    # qBittorrent's Web UI, reached over the compose bridge (it lives in gluetun's namespace).
    qbit_url: str = "http://gluetun:8080"
    qbit_user: str = "admin"
    # A secret, like the Jellyfin key: never in repr/str or a log line.
    qbit_password: str = field(default="", repr=False)
    # File mode mask for the folders the organizer creates (UMASK in .env; 002 keeps the group
    # able to write, which the other media users need). Applied once at startup, see __main__.
    umask: int = 0o022
    # The watchdog's status page (it lives in its own container; the UI badge reads it, #22).
    watchdog_status_url: str = "http://watchdog:8090"
    # The reverse proxy (the compose service "proxy") is the only peer whose X-Forwarded-For is
    # believed, for the login brake (#76). EMPTY = believe no header.
    trusted_proxy: str = "proxy"
    # Where yt-dlp's requests go out: gluetun's HTTP proxy by default, so YouTube is asked
    # through the VPN. EMPTY = ask directly from the home line. See docs/youtube.md.
    ytdlp_proxy: str = "http://gluetun:8888"
    youtube_probe_timeout: float = 25.0
    # Tallest YouTube video to download, in pixels (0 = no limit): a disk-space knob.
    youtube_max_height: int = 1080
    # How often a subscribed channel's feed is looked at, in minutes (0 = never; see #20).
    subscription_poll_minutes: int = 60
    # Where podcast feeds and episodes are fetched through (#34): gluetun's HTTP proxy by default,
    # so the VPN carries them too. EMPTY = fetch directly. Only http:// proxies are supported.
    podcast_proxy: str = "http://gluetun:8888"
    # How often a subscribed podcast's feed is looked at, in minutes (0 = never). Slower than the
    # YouTube channels: a podcast publishes a few times a week at most.
    podcast_poll_minutes: int = 240
    # Give up on a magnet link with no progress for this many days; 0 = never (#40).
    magnet_stall_days: int = 7
    # The first admin account (#44): created from these at start if it does not exist, and its
    # password is reset to this hash whenever the hash in .env changes (the way to recover a lost
    # admin password). The hash is made by scripts/app/set_admin.py; empty = no bootstrap.
    admin_user: str = "admin"
    admin_password_hash: str = field(default="", repr=False)
    # The dashboard's "internet" check opens a TCP connection to this host:port every 30 s while
    # a dashboard is open (#63). A host the app contacts anyway; EMPTY = check off (reported as
    # "unknown", never "ok"). Only the host name is ever logged.
    internet_check_host: str = "www.youtube.com"
    internet_check_port: int = 443
    # Networks the app never fetches from (podcasts): HOME_PUBLIC_IP plus FETCH_BLOCKED_NETWORKS,
    # i.e. the home network's own global IPv6 prefixes. Semi-secret like HOME_PUBLIC_IP: never in
    # repr/str or a log line, only the count (see `summary`).
    fetch_blocked_networks: tuple = field(default=(), repr=False)

    @property
    def qbit_configured(self) -> bool:
        return bool(self.qbit_url and self.qbit_user and self.qbit_password)

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @property
    def database_path(self) -> Path:
        """The SQLite file, in the /config bind mount (./config/app on the host)."""
        return self.config_dir / "app.db"

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
            "qbit_url": self.qbit_url,
            "qbit_user": self.qbit_user,
            "qbit_configured": self.qbit_configured,
            "trusted_proxy": self.trusted_proxy or None,
            "fetch_blocked_networks": len(self.fetch_blocked_networks),
            "internet_check": (
                f"{self.internet_check_host}:{self.internet_check_port}"
                if self.internet_check_host
                else None
            ),
            "admin_user": self.admin_user,
            "admin_bootstrap": bool(self.admin_password_hash),
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

        qbit_url = text("QBIT_URL", "http://gluetun:8080").rstrip("/")
        parsed_qbit = urlparse(qbit_url)
        if parsed_qbit.scheme not in {"http", "https"} or not parsed_qbit.hostname:
            problems.append(f"QBIT_URL must be an http(s) address, got {qbit_url!r}")
        elif parsed_qbit.path.strip("/"):
            problems.append(f"QBIT_URL must be host[:port] without a path, got {qbit_url!r}")

        umask = 0o022
        raw_umask = text("UMASK", "022")
        if re.fullmatch(r"0?[0-7]{3}", raw_umask):
            umask = int(raw_umask, 8)
        else:
            problems.append(f"UMASK must be an octal mask like 002, got {raw_umask!r}")

        watchdog_url = text("WATCHDOG_STATUS_URL", "http://watchdog:8090").rstrip("/")
        parsed_watchdog = urlparse(watchdog_url)
        if parsed_watchdog.scheme != "http" or not parsed_watchdog.hostname:
            problems.append(f"WATCHDOG_STATUS_URL must be an http address, got {watchdog_url!r}")
        elif parsed_watchdog.path.strip("/"):
            problems.append(
                f"WATCHDOG_STATUS_URL must be host[:port] without a path, got {watchdog_url!r}"
            )

        raw_trusted = env.get("TRUSTED_PROXY")  # unset = default, set but empty = trust nobody
        trusted_proxy = "proxy" if raw_trusted is None else raw_trusted.strip()
        if trusted_proxy and not valid_host(trusted_proxy):
            problems.append("TRUSTED_PROXY must be a host name or IP address, or empty")

        raw_proxy = env.get("YTDLP_PROXY")  # unset = default, set but empty = no proxy
        proxy = "http://gluetun:8888" if raw_proxy is None else raw_proxy.strip()
        if proxy and urlparse(proxy).scheme not in {"http", "https", "socks5", "socks5h"}:
            problems.append("YTDLP_PROXY must be an http(s) or socks5 address, or empty")
        raw_timeout = text("YOUTUBE_PROBE_TIMEOUT", "25")
        try:
            probe_timeout = float(raw_timeout)
        except ValueError:
            probe_timeout = 25.0
            problems.append(
                f"YOUTUBE_PROBE_TIMEOUT must be a number of seconds, got {raw_timeout!r}"
            )
        else:
            if not 3 <= probe_timeout <= 120:
                problems.append("YOUTUBE_PROBE_TIMEOUT must be between 3 and 120 seconds")

        raw_height = text("YOUTUBE_MAX_HEIGHT", "1080")
        max_height = 1080
        if raw_height.isdigit() and (int(raw_height) == 0 or 144 <= int(raw_height) <= 4320):
            max_height = int(raw_height)
        else:
            problems.append("YOUTUBE_MAX_HEIGHT must be 0 (no limit) or between 144 and 4320")

        raw_poll = text("SUBSCRIPTION_POLL_MINUTES", "60")
        poll_minutes = 60
        if raw_poll.isdigit() and (int(raw_poll) == 0 or 15 <= int(raw_poll) <= 1440):
            poll_minutes = int(raw_poll)
        else:
            problems.append("SUBSCRIPTION_POLL_MINUTES must be 0 (off) or between 15 and 1440")

        raw_podcast_proxy = env.get("PODCAST_PROXY")  # unset = default, set but empty = direct
        podcast_proxy = (
            "http://gluetun:8888" if raw_podcast_proxy is None else raw_podcast_proxy.strip()
        )
        if podcast_proxy:
            parsed_proxy = urlparse(podcast_proxy)
            if parsed_proxy.scheme != "http" or not parsed_proxy.hostname:
                problems.append("PODCAST_PROXY must be an http:// address (host:port), or empty")

        raw_podcast_poll = text("PODCAST_POLL_MINUTES", "240")
        podcast_poll = 240
        if raw_podcast_poll.isdigit() and (
            int(raw_podcast_poll) == 0 or 30 <= int(raw_podcast_poll) <= 1440
        ):
            podcast_poll = int(raw_podcast_poll)
        else:
            problems.append("PODCAST_POLL_MINUTES must be 0 (off) or between 30 and 1440")

        raw_stall = text("MAGNET_STALL_DAYS", "7")
        stall_days = 7
        if raw_stall.isdigit() and 0 <= int(raw_stall) <= 365:
            stall_days = int(raw_stall)
        else:
            problems.append("MAGNET_STALL_DAYS must be 0 (never) or a number of days up to 365")

        admin_user = text("APP_ADMIN_USER", "admin")
        if accounts.check_username(admin_user):
            problems.append("APP_ADMIN_USER must be 3 to 64 characters: letters, digits, . _ -")
        admin_hash = text("APP_ADMIN_PASSWORD_HASH", "")
        if admin_hash == "changeme":
            admin_hash = ""  # the template's placeholder: nothing set yet
        elif admin_hash and not passwords.is_hash(admin_hash):
            problems.append(
                "APP_ADMIN_PASSWORD_HASH is not a hash made by scripts/app/set_admin.py"
            )

        raw_check_host = env.get("INTERNET_CHECK_HOST")  # unset = default, set but empty = off
        check_host = "www.youtube.com" if raw_check_host is None else raw_check_host.strip()
        if check_host and not valid_host(check_host):
            problems.append("INTERNET_CHECK_HOST must be a host name or IP address, or empty")
        raw_check_port = text("INTERNET_CHECK_PORT", "443")
        check_port = 443
        if raw_check_port.isdigit() and 1 <= int(raw_check_port) <= 65535:
            check_port = int(raw_check_port)
        else:
            problems.append(
                f"INTERNET_CHECK_PORT must be a port number (1-65535), got {raw_check_port!r}"
            )

        blocked: list = []
        for name in ("HOME_PUBLIC_IP", "FETCH_BLOCKED_NETWORKS"):
            try:
                blocked.extend(parse_networks(env.get(name, "")))
            except ValueError as exc:  # names the entry's position, never its value
                problems.append(f"{name}: {exc}")

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
            qbit_url=qbit_url,
            qbit_user=text("QBIT_USER", "admin"),
            qbit_password=text("QBIT_PASSWORD"),
            umask=umask,
            watchdog_status_url=watchdog_url,
            trusted_proxy=trusted_proxy,
            ytdlp_proxy=proxy,
            youtube_probe_timeout=probe_timeout,
            youtube_max_height=max_height,
            subscription_poll_minutes=poll_minutes,
            podcast_proxy=podcast_proxy,
            podcast_poll_minutes=podcast_poll,
            magnet_stall_days=stall_days,
            admin_user=admin_user,
            admin_password_hash=admin_hash,
            internet_check_host=check_host,
            internet_check_port=check_port,
            fetch_blocked_networks=tuple(blocked),
        )


HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def valid_host(value: str) -> bool:
    """A host name (letters, digits, hyphens, dots) or an IP address; no scheme, port or path."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return True
    return len(value) <= 253 and all(HOST_LABEL.fullmatch(label) for label in value.split("."))


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
