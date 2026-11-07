"""The page's translation table, parsed from index.html."""

from __future__ import annotations

import json
import re


def translations(html: str) -> dict[str, dict[str, str]]:
    match = re.search(
        r'<script type="application/json" id="i18n">\s*(\{.*?\})\s*</script>', html, re.S
    )
    assert match, "index.html has no i18n table"
    return json.loads(match.group(1))
