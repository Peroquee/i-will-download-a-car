"""Read docker-compose.yml and .env files the way the tests need them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?msg}, ${VAR?msg}, ${VAR:+alt}
_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<op>:?[-?+])?(?P<arg>[^}]*)\}")


def load_compose(path: Path) -> dict[str, Any]:
    """Parse a compose file; YAML merge keys (<<: *anchor) are resolved by PyYAML."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "services" not in data:
        raise ValueError(f"{path} has no top-level 'services'")
    return data


def vpn_variant_files(compose_path: Path) -> dict[str, Path]:
    """The per-VPN-type files gluetun `extends`: {"openvpn": .../compose/vpn-openvpn.yml, ...}."""
    folder = compose_path.parent / "compose"
    return {f.stem.removeprefix("vpn-"): f for f in sorted(folder.glob("vpn-*.yml"))}


def gluetun_environment(compose_path: Path, vpn_type: str) -> dict[str, Any]:
    """gluetun's environment as Compose merges it: the variant file under the base file."""
    variant = load_compose(vpn_variant_files(compose_path)[vpn_type])
    base = load_compose(compose_path)
    return {
        **environment_as_dict(services(variant)["gluetun"]),
        **environment_as_dict(services(base)["gluetun"]),
    }


def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return compose["services"]


def default_services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Services that start with a plain `docker compose up` (no profile)."""
    return {name: svc for name, svc in services(compose).items() if not svc.get("profiles")}


def referenced_variables(text: str) -> dict[str, str | None]:
    """Map each interpolated variable name to its operator (None, ':-', ':?', ...)."""
    found: dict[str, str | None] = {}
    for match in _INTERPOLATION.finditer(text):
        name, op = match.group("name"), match.group("op")
        # A required (?) use anywhere wins over an optional use elsewhere.
        if name not in found or (op and "?" in op):
            found[name] = op
    return found


def iter_strings(node: Any):
    """Every string value in a parsed YAML tree (comments are already gone)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from iter_strings(key)
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_strings(value)


def compose_variables(compose: dict[str, Any]) -> dict[str, str | None]:
    """Variables Compose will interpolate, read from values only (not comments)."""
    return referenced_variables("\n".join(iter_strings(compose)))


def all_compose_variables(compose_path: Path) -> dict[str, str | None]:
    """compose_variables of the main file plus every file it extends (compose/vpn-*.yml)."""
    found = compose_variables(load_compose(compose_path))
    for variant in vpn_variant_files(compose_path).values():
        for name, op in compose_variables(load_compose(variant)).items():
            if name not in found or (op and "?" in op):
                found[name] = op
    return found


def blank_interpolations(value: str, replacement: str = "X") -> str:
    """Replace every ${...} with a placeholder, to inspect a value's shape."""
    return _INTERPOLATION.sub(replacement, value)


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser: KEY=VALUE lines, comments and blanks ignored."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: not KEY=VALUE: {raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}:{lineno}: invalid variable name {key!r}")
        if key in values:
            raise ValueError(f"{path}:{lineno}: duplicate variable {key}")
        values[key] = value.strip().strip('"').strip("'")
    return values


def environment_as_dict(service: dict[str, Any]) -> dict[str, Any]:
    """Compose allows environment as a mapping or as a list of KEY=VALUE strings."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    return dict(env)
