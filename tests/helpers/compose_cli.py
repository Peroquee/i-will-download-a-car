"""Run `docker compose` against the repo's compose file with a controlled environment.

`docker compose config` needs only the CLI and the Compose plugin, not a daemon,
so these helpers work on any machine that has them installed.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tests.helpers.compose import parse_env_file

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


@functools.cache
def compose_cli_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def clean_env() -> dict[str, str]:
    """Process env without any variable the compose file uses.

    Compose lets shell variables override the env file, so a stray TZ or PUID
    in CI would silently change what we test.
    """
    names = set(parse_env_file(ENV_EXAMPLE))
    env = {k: v for k, v in os.environ.items() if k not in names}
    env.pop("COMPOSE_PROFILES", None)
    env.pop("COMPOSE_FILE", None)
    env.pop("COMPOSE_PROJECT_NAME", None)
    return env


def write_env(path: Path, **overrides: str | None) -> Path:
    """Copy .env.example to `path`, replacing (or with None, dropping) variables."""
    values = parse_env_file(ENV_EXAMPLE)
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def compose(
    *args: str, env_file: Path = ENV_EXAMPLE, profiles: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "--project-directory", str(REPO_ROOT), "-f", str(COMPOSE_FILE)]
    cmd += ["--env-file", str(env_file)]
    for profile in profiles:
        cmd += ["--profile", profile]
    return subprocess.run(
        [*cmd, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=clean_env(),
        timeout=60,
        check=False,
    )


def rendered_config(env_file: Path = ENV_EXAMPLE, profiles: tuple[str, ...] = ()) -> dict[str, Any]:
    """The fully interpolated config as Compose itself understands it."""
    result = compose("config", "--format", "json", env_file=env_file, profiles=profiles)
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed:\n{result.stderr}")
    return json.loads(result.stdout)


def normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    """The fields of a rendered config that we own, in a form stable across Compose versions.

    Used by the regression snapshot and by the integration test that writes the
    actual config to reports/ for review.
    """

    def path(value: str) -> str:
        return value.replace(str(REPO_ROOT), "<repo>")

    def device(d: Any) -> str:
        return d if isinstance(d, str) else f"{d['source']}:{d['target']}"

    out: dict[str, Any] = {}
    for name, svc in sorted(config["services"].items()):
        out[name] = {
            "image": svc.get("image"),
            "restart": svc.get("restart"),
            "profiles": svc.get("profiles", []),
            "network_mode": svc.get("network_mode"),
            "user": svc.get("user"),
            "cap_add": sorted(svc.get("cap_add", [])),
            "devices": sorted(device(d) for d in svc.get("devices", [])),
            "ports": sorted(
                f"{p.get('host_ip', '')}:{p.get('published')}:{p['target']}/{p.get('protocol')}"
                for p in svc.get("ports", [])
            ),
            "environment": dict(sorted((svc.get("environment") or {}).items())),
            "volumes": sorted(
                f"{path(v['source'])}:{v['target']}{':ro' if v.get('read_only') else ''}"
                for v in svc.get("volumes", [])
            ),
            "depends_on": {
                dep: spec.get("condition") for dep, spec in (svc.get("depends_on") or {}).items()
            },
            "healthcheck": svc.get("healthcheck"),
            "logging": svc.get("logging"),
        }
    return out
