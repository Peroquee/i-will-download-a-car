"""Resolution choices, the resolution right, and which cap applies (issues #47, #65).

The values are the fixed list of #47: 0 = best (no cap), or one of 2160, 1440, 1080, 720, 480,
360. `None` means "not set" and falls back to the next level.

* **A manual download** uses the value the person chose, but only when their account may choose
  resolutions; otherwise it uses the account's `default_max_height`, and if that is not set the
  server default (`YOUTUBE_MAX_HEIGHT`).
* **A subscription video** uses the subscription's `max_height`, else the server default.
  Subscriptions are shared, not per user.
* **The right** is `accounts.can_set_resolution`, which an admin sets per account. Admins may
  always choose resolutions.

This module imports nothing from the rest of the app, so `accounts`, the API modules and the
database layer can all use it without an import cycle.
"""

from __future__ import annotations

MAX_HEIGHTS = (0, 2160, 1440, 1080, 720, 480, 360)
DETAIL = "max_height must be 0 (best) or one of " + ", ".join(
    str(height) for height in MAX_HEIGHTS[1:]
)
NOT_ALLOWED = (
    "you may not choose a resolution; your account uses the resolution an admin set for it"
)


class NotAllowed(Exception):
    """The account may not choose a resolution and asked for a different one."""


def valid(value: object) -> bool:
    """True for a value the API accepts. `True`/`False` are not resolutions (bool is an int)."""
    return not isinstance(value, bool) and isinstance(value, int) and value in MAX_HEIGHTS


def may_choose(is_admin: bool, can_set_resolution: bool) -> bool:
    """Admins may always choose; everyone else needs the right the admin gave them."""
    return is_admin or can_set_resolution


def effective_default(default_max_height: int | None, server_default: int) -> int:
    """The resolution a manual download of this account uses when it sends no choice."""
    return server_default if default_max_height is None else default_max_height


def for_download(
    *,
    allowed: bool,
    default_max_height: int | None,
    chosen: int | None,
    server_default: int,
) -> int | None:
    """The `max_height` to store in a job; None means the download's configured default applies.

    A choice is used only when the account may choose (or when it is already the account's
    effective default: sending the same value changes nothing). Otherwise `NotAllowed` says why
    the request is refused, so the API can answer 403 instead of silently ignoring the value.
    """
    if chosen is not None:
        if allowed:
            return chosen
        if chosen != effective_default(default_max_height, server_default):
            raise NotAllowed(NOT_ALLOWED)
    return default_max_height
