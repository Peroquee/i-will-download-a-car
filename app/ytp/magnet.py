"""Magnet link validation and the infohash (issues #10, #13).

A magnet link comes from a user's text box, so it is untrusted. qBittorrent's `urls` form
field takes SEVERAL links separated by newlines and also accepts plain http(s) URLs, which it
will fetch: one smuggled line would make qBittorrent download an arbitrary address. So this
module accepts only a single, whitespace-free `magnet:?` link with exactly one BitTorrent v1
infohash (`xt=urn:btih:`), and nothing else. Callers strip surrounding whitespace first
(a pasted trailing newline is not a reason to refuse).

Pure v2 magnets (`xt=urn:btmh:` only) are refused: the stack's dedupe key is the v1 infohash.
Hybrid links that carry both are fine.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qsl

MAX_LENGTH = 4096  # real links with many trackers are a few hundred characters

_HEX_HASH = re.compile(r"[0-9a-fA-F]{40}")
_BASE32_HASH = re.compile(r"[A-Za-z2-7]{32}")


class InvalidMagnet(ValueError):
    """The text is not a magnet link this stack accepts. The message is safe to show a user."""


def infohash_from_magnet(magnet: str) -> str:
    """The torrent's v1 infohash as 40 lower-case hex characters; raises InvalidMagnet.

    Accepts the 40-character hex form and the 32-character base32 form (older links).
    """
    if not isinstance(magnet, str) or not magnet:
        raise InvalidMagnet("the magnet link is empty")
    if len(magnet) > MAX_LENGTH:
        raise InvalidMagnet(f"the magnet link is longer than {MAX_LENGTH} characters")
    if not magnet.isprintable() or any(ch.isspace() for ch in magnet):
        # Newlines would let a second URL ride along (see the module docstring).
        raise InvalidMagnet("the magnet link must be a single line without spaces")
    scheme, _, query = magnet.partition("?")
    if scheme.lower() != "magnet:" or not query:
        raise InvalidMagnet("the link must start with magnet:?")
    raw_hashes = [
        value[len("urn:btih:") :]
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() == "xt" and value.lower().startswith("urn:btih:")
    ]
    if not raw_hashes:
        raise InvalidMagnet("the magnet link has no BitTorrent infohash (xt=urn:btih:...)")
    # Normalised first, so the same hash written as hex and as base32 counts as one.
    hashes = {_normalize(raw) for raw in raw_hashes}
    if len(hashes) > 1:
        raise InvalidMagnet("the magnet link names more than one torrent")
    return hashes.pop()


def _normalize(raw: str) -> str:
    if _HEX_HASH.fullmatch(raw):
        return raw.lower()
    if _BASE32_HASH.fullmatch(raw):
        return base64.b32decode(raw.upper()).hex()
    raise InvalidMagnet("the infohash must be 40 hex or 32 base32 characters")
