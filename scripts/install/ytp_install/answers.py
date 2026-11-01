"""What the installer asks, how each answer is checked, and which settings it becomes.

Pure functions and one dataclass, no terminal and no files, so every rule is a unit test.
A checker returns None when the value is fine, else the key of the sentence in `texts` that
explains what is wrong (the caller shows it and asks again).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse

LIBRARY_DEFAULTS = {
    "MEDIA_DIR_MOVIES": "Filme",
    "MEDIA_DIR_SERIES": "Serien",
    "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
    "MEDIA_DIR_MUSIC": "Musik",
    "MEDIA_DIR_PODCASTS": "Podcasts",
    "MEDIA_DIR_YOUTUBE": "Youtube",
}
USER_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,64}")
PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9 .-]{1,40}")
MIN_WEB_PASSWORD = 12
MIN_QBIT_PASSWORD = 10
# Settings whose value must never be shown, logged or put on a command line.
SECRET_KEYS = frozenset(
    {"OPENVPN_PASSWORD", "WIREGUARD_PRIVATE_KEY", "QBIT_PASSWORD", "JELLYFIN_API_KEY", "ALERT_URL"}
)


@dataclass
class Answers:
    """Everything the installer needs to know. The three `*_password`/`web_password` fields that
    are not settings (web login) are used once and never stored."""

    language: str = "de"
    timezone: str = "Europe/Berlin"
    puid: int = 1000
    pgid: int = 1000
    data_root: str = "/data"
    dirs: dict[str, str] = field(default_factory=lambda: dict(LIBRARY_DEFAULTS))
    lan_address: str = ""
    lan_subnet: str = ""
    web_user: str = "admin"
    web_password: str = ""  # hashed by Caddy, never written anywhere
    # "ovpn": the provider gives a .ovpn file (ExpressVPN); "builtin": a gluetun provider by name
    vpn_kind: str = "ovpn"
    vpn_provider: str = ""  # builtin only, e.g. "mullvad"
    vpn_type: str = "openvpn"
    vpn_user: str = ""
    vpn_password: str = ""
    wireguard_key: str = ""
    wireguard_addresses: str = ""
    vpn_countries: str = ""
    ovpn_source: str = ""  # where the user's .ovpn file is now; copied to config/expressvpn.ovpn
    qbit_user: str = "admin"
    qbit_password: str = ""
    jellyfin_url: str = ""
    jellyfin_key: str = ""
    alert_url: str = ""
    home_prefix: str = ""

    def env_values(self) -> dict[str, str]:
        """The .env settings this installer owns, in a stable order. Settings that do not apply
        (the other VPN type's credentials) are left out, so an existing value there survives."""
        values: dict[str, str] = {
            "TZ": self.timezone,
            "PUID": str(self.puid),
            "PGID": str(self.pgid),
            "DATA_ROOT": self.data_root,
            **self.dirs,
            "LAN_SUBNET": self.lan_subnet,
            "PROXY_BIND": self.lan_address,
            "PROXY_USER": self.web_user,
            "QBIT_USER": self.qbit_user,
            "QBIT_PASSWORD": self.qbit_password,
            "JELLYFIN_URL": self.jellyfin_url,
            "JELLYFIN_API_KEY": self.jellyfin_key,
            "ALERT_URL": self.alert_url,
            "HOME_PUBLIC_IP": self.home_prefix,
        }
        if self.vpn_kind == "ovpn":
            values["VPN_SERVICE_PROVIDER"] = "custom"
            values["VPN_TYPE"] = "openvpn"
            values["OPENVPN_CUSTOM_CONFIG"] = "/gluetun/custom/expressvpn.ovpn"
            values["OPENVPN_USER"] = self.vpn_user
            values["OPENVPN_PASSWORD"] = self.vpn_password
            values["VPN_SERVER_COUNTRIES"] = ""
            values["VPN_SERVER_CITIES"] = ""
        else:
            values["VPN_SERVICE_PROVIDER"] = self.vpn_provider
            values["VPN_TYPE"] = self.vpn_type
            values["OPENVPN_CUSTOM_CONFIG"] = ""
            values["VPN_SERVER_COUNTRIES"] = self.vpn_countries
            values["VPN_SERVER_CITIES"] = ""
            if self.vpn_type == "wireguard":
                values["WIREGUARD_PRIVATE_KEY"] = self.wireguard_key
                values["WIREGUARD_ADDRESSES"] = self.wireguard_addresses
            else:
                values["OPENVPN_USER"] = self.vpn_user
                values["OPENVPN_PASSWORD"] = self.vpn_password
        return values

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Answers:
        """Best effort: the answers an existing .env stands for (secrets included, they are read
        from the file the installer wrote itself). Missing or odd values keep the defaults."""
        answers = cls()

        def get(key: str, default: str = "") -> str:
            value = env.get(key, "")
            return value if value and value != "changeme" else default

        answers.timezone = get("TZ", answers.timezone)
        for key, attr in (("PUID", "puid"), ("PGID", "pgid")):
            if get(key).isdigit():
                setattr(answers, attr, int(get(key)))
        answers.data_root = get("DATA_ROOT", answers.data_root)
        answers.dirs = {k: get(k, v) for k, v in LIBRARY_DEFAULTS.items()}
        answers.lan_subnet = get("LAN_SUBNET")
        answers.lan_address = get("PROXY_BIND")
        answers.web_user = get("PROXY_USER", answers.web_user)
        answers.qbit_user = get("QBIT_USER", answers.qbit_user)
        answers.qbit_password = get("QBIT_PASSWORD")
        answers.jellyfin_url = get("JELLYFIN_URL")
        answers.jellyfin_key = get("JELLYFIN_API_KEY")
        answers.alert_url = get("ALERT_URL")
        answers.home_prefix = get("HOME_PUBLIC_IP")
        provider = get("VPN_SERVICE_PROVIDER", "custom")
        answers.vpn_kind = "ovpn" if provider == "custom" else "builtin"
        answers.vpn_provider = "" if provider == "custom" else provider
        answers.vpn_type = get("VPN_TYPE", "openvpn")
        answers.vpn_user = get("OPENVPN_USER")
        answers.vpn_password = get("OPENVPN_PASSWORD")
        answers.wireguard_key = get("WIREGUARD_PRIVATE_KEY")
        answers.wireguard_addresses = get("WIREGUARD_ADDRESSES")
        answers.vpn_countries = get("VPN_SERVER_COUNTRIES")
        return answers

    def masked(self) -> dict[str, str]:
        """The settings with every secret replaced, for the summary the user confirms."""
        return {
            key: ("(set)" if value else "") if key in SECRET_KEYS else value
            for key, value in self.env_values().items()
        }


ANSWER_FIELDS = tuple(f.name for f in fields(Answers))

# ---------------------------------------------------------------------------- checkers


def check_user(value: str) -> str | None:
    return None if USER_PATTERN.fullmatch(value) else "err.user"


def check_text_secret(value: str, minimum: int) -> str | None:
    """A password-like value that has to survive being written into .env and read by Docker
    Compose: no quote, no line break, no control character, no leading/trailing space."""
    if "'" in value:
        return "err.quote"
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "err.control"
    if len(value) < minimum:
        return "err.short"
    return None


def check_web_password(value: str) -> str | None:
    return check_text_secret(value, MIN_WEB_PASSWORD)


def check_qbit_password(value: str) -> str | None:
    return check_text_secret(value, MIN_QBIT_PASSWORD)


def check_vpn_secret(value: str) -> str | None:
    if not value:
        return "err.empty"
    return check_text_secret(value, 1)


def check_vpn_user(value: str) -> str | None:
    if not value:
        return "err.empty"
    return check_text_secret(value, 1)


def valid_folder_name(name: str) -> bool:
    """One path component: nothing that could leave DATA_ROOT or be read as an option (the same
    rule as the app's config and scripts/host/create-layout.sh)."""
    return (
        name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and not name.startswith("-")
        and name.isprintable()
        and "'" not in name
    )


def check_folder_names(dirs: dict[str, str]) -> str | None:
    if not all(valid_folder_name(name) for name in dirs.values()):
        return "err.folder"
    if len(set(dirs.values())) != len(dirs):
        return "err.folder_twice"
    if "downloads" in dirs.values():
        return "err.folder_downloads"
    return None


def check_data_root(value: str) -> str | None:
    if not value.startswith("/") or value == "/" or "'" in value or not value.isprintable():
        return "err.path"
    if any(part in {"", ".", ".."} for part in value.rstrip("/").split("/")[1:]):
        return "err.path"
    return None


def check_ipv4(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "err.ip"
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        return "err.ip"
    return None


def check_subnet(value: str) -> str | None:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return "err.subnet"
    if network.version != 4 or network.prefixlen < 8 or network.prefixlen > 30:
        return "err.subnet"
    return None


def check_address_in_subnet(address: str, subnet: str) -> str | None:
    try:
        if ipaddress.ip_address(address) not in ipaddress.ip_network(subnet, strict=True):
            return "err.not_in_subnet"
    except ValueError:
        return "err.ip"
    return None


def check_http_url(value: str) -> str | None:
    if "'" in value or any(ch.isspace() for ch in value):
        return "err.url"
    parts = urlparse(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "err.url"
    return None


def check_jellyfin_key(value: str) -> str | None:
    return None if re.fullmatch(r"[0-9a-fA-F]{32}", value) else "err.jellyfin_key"


def check_prefixes(value: str) -> str | None:
    """HOME_PUBLIC_IP: addresses or prefixes separated by commas or spaces."""
    for item in value.replace(",", " ").split():
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            return "err.prefix"
    return None


def check_provider(value: str) -> str | None:
    return None if PROVIDER_PATTERN.fullmatch(value) else "err.provider"


def check_countries(value: str) -> str | None:
    if "'" in value or not value.isprintable():
        return "err.countries"
    return None


def check_wireguard_addresses(value: str) -> str | None:
    for item in value.replace(",", " ").split():
        try:
            ipaddress.ip_interface(item)
        except ValueError:
            return "err.wg_addresses"
    return None if value.strip() else "err.empty"


def normalise_prefixes(value: str) -> str:
    return ", ".join(value.replace(",", " ").split())


def check_timezone(value: str, known: bool) -> str | None:
    return None if known else "err.timezone"
