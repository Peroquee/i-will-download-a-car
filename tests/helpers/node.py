"""Node and the loud skip policy shared by the browser-side tests."""

from __future__ import annotations

import os
import shutil

import pytest

NODE = shutil.which("node")
NODE_MISSING = (
    "node is NOT installed: the browser-side tests are SKIPPED and prove nothing. "
    "Install it (apt install nodejs)."
)
# Locally a missing node skips with the loud reason above. In CI (GitLab sets CI) the tests are
# NOT skipped: they reach require_node and fail, so a pipeline cannot go green without them.
needs_node = pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason=NODE_MISSING)


def require_node() -> str:
    if NODE is None:
        pytest.fail(NODE_MISSING + " The test:unit job installs it in its before_script.")
    return NODE
