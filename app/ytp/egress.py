"""The slow egress sanity check (issue #23): is the address the VPN shows the world a VPN's?

The watchdog (#21) answers "is the tunnel up". This answers a subtler question every few minutes:
"is traffic really leaving through the VPN?" It uses what is already in the stack and adds no
third-party IP service: gluetun knows its own public address and where it is
(`GET /v1/publicip/ip` on the control server, with the watchdog's read-only key).

ExpressVPN publishes no address ranges, so "inside the provider's ranges" cannot be checked.
What can be checked, and is:

* the address is a **public** one (not private, loopback, link-local or carrier-grade NAT);
* it is **not your home line's**: `HOME_PUBLIC_IP` lists the home network's public addresses or
  prefixes (see docs/watchdog.md for how to find them; for a DS-Lite line the native IPv6 prefix
  is the one that matters, the IPv4 is the provider's shared address);
* if `EGRESS_EXPECTED_COUNTRY` is set, gluetun's reported **country** is one of those;
* qBittorrent is **bound to the VPN interface** (`tun0`): every socket it opens, IPv4 and IPv6,
  goes through the tunnel or nowhere. That is the practical IPv6-leak guard from inside the
  stack, next to gluetun's own firewall.

Anything gluetun cannot answer (the tunnel restarting) is *inconclusive*, never a mismatch.
Addresses are never written in full: logs get `203.0.x.x`, alerts and the status get none.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .clients.gluetun import GluetunAuthError, GluetunControl, GluetunUnreachable
from .clients.qbittorrent import QbitClient, QbitError

VPN_INTERFACE = "tun0"


def parse_networks(text: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """`HOME_PUBLIC_IP`: addresses or CIDR prefixes separated by commas or spaces.

    The error never repeats a value (they are semi-secret): it only says which entry is bad.
    """
    networks = []
    for index, item in enumerate(text.replace(",", " ").split(), start=1):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"entry {index} is not an IP address or network") from None
    return tuple(networks)


def mask_ip(value: str) -> str:
    """`203.0.113.7` becomes `203.0.x.x`, `2003:abcd:1234::1` becomes `2003:abcd:x`."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "x"
    if address.version == 4:
        first, second, *_ = str(address).split(".")
        return f"{first}.{second}.x.x"
    first, second, *_ = address.exploded.split(":")
    return f"{first}:{second}:x"


@dataclass(frozen=True)
class EgressResult:
    status: str  # "ok" | "mismatch" | "inconclusive"
    problems: list[str] = field(default_factory=list)  # readable, never containing an address
    country: str = ""
    organization: str = ""
    masked_ip: str = ""


class EgressChecker:
    def __init__(
        self,
        control: GluetunControl,
        qbit: QbitClient | None,
        *,
        home_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
        expected_countries: tuple[str, ...] = (),
    ):
        self.control = control
        self.qbit = qbit
        self.home_networks = home_networks
        self.expected_countries = tuple(c.casefold() for c in expected_countries)

    def check(self) -> EgressResult:
        try:
            info = self.control.public_ip()
        except GluetunAuthError as exc:
            return EgressResult("inconclusive", [str(exc)])
        except GluetunUnreachable as exc:
            return EgressResult("inconclusive", [str(exc)])
        try:
            address = ipaddress.ip_address(info.ip)
        except ValueError:
            return EgressResult("inconclusive", ["gluetun reported an unreadable address"])
        masked = mask_ip(info.ip)

        problems: list[str] = []
        if not address.is_global:
            problems.append("the egress address is not a public address")
        if any(
            address in network
            for network in self.home_networks
            if network.version == address.version
        ):
            problems.append(
                "the egress address belongs to your home network "
                "(traffic is NOT leaving through the VPN)"
            )
        if self.expected_countries:
            if not info.country:
                return EgressResult(
                    "inconclusive", ["gluetun reports no country"], masked_ip=masked
                )
            if info.country.casefold() not in self.expected_countries:
                problems.append(
                    f"the egress country is {info.country}, expected "
                    + " or ".join(sorted(self.expected_countries))
                )
        problems += self._binding_problems()
        return EgressResult(
            "mismatch" if problems else "ok",
            problems,
            country=info.country,
            organization=info.organization,
            masked_ip=masked,
        )

    def _binding_problems(self) -> list[str]:
        if self.qbit is None:
            return []
        try:
            bound = self.qbit.preferences().get("current_network_interface")
        except QbitError:
            return []  # qBittorrent being away is the reconciler's and watchdog's business
        if bound != VPN_INTERFACE:
            return [f"qBittorrent is not bound to the VPN interface ({VPN_INTERFACE})"]
        return []
