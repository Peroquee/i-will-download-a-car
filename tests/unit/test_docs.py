"""The documentation stays true to the repository (issue #31): links resolve, every setting is described."""

# ruff: noqa: E501
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.compose import all_compose_variables, parse_env_file

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md")])
CONFIGURATION = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")


def slug(heading: str) -> str:
    """GitLab's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
    return re.sub(r"-+", "-", text)


def anchors(path: Path) -> set[str]:
    in_fence = False
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and (match := re.match(r"#{1,6}\s+(.*)", line)):
            found.add(slug(match.group(1)))
    return found


def links(path: Path):
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    return re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_and_anchor_resolves(page):
    for target in links(page):
        if re.match(r"[a-z]+:", target):
            continue  # an external address
        file_part, _, fragment = target.partition("#")
        destination = (page.parent / file_part).resolve() if file_part else page
        assert destination.exists(), f"{page.name}: {target} does not exist"
        if fragment and destination.suffix == ".md":
            assert slug(fragment) in anchors(destination), f"{page.name}: no heading for {target}"


def test_every_page_is_listed_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for page in (ROOT / "docs").glob("*.md"):
        assert f"docs/{page.name}" in readme, f"README does not list docs/{page.name}"


# --- the configuration reference ---------------------------------------------------------------------


def documented(name: str) -> bool:
    return bool(re.search(rf"`{re.escape(name)}`", CONFIGURATION))


def test_every_variable_of_env_example_is_documented():
    missing = [n for n in parse_env_file(ROOT / ".env.example") if not documented(n)]
    assert not missing, f"docs/configuration.md lacks: {missing}"


def test_every_variable_of_the_compose_file_is_documented():
    missing = [n for n in all_compose_variables(ROOT / "docker-compose.yml") if not documented(n)]
    assert not missing, f"docs/configuration.md lacks: {missing}"


def test_every_variable_the_app_code_reads_is_documented():
    names: set[str] = set()
    for module in (ROOT / "app" / "ytp").glob("*.py"):
        text = module.read_text(encoding="utf-8")
        names |= set(re.findall(r"""(?:text|number|get)\(\s*["']([A-Z][A-Z0-9_]{3,})["']""", text))
        names |= set(re.findall(r"""environ\.get\(\s*["']([A-Z][A-Z0-9_]{3,})["']""", text))
    names |= {"APP_HOST", "APP_PORT"}
    missing = sorted(n for n in names if not documented(n))
    assert not missing, f"docs/configuration.md lacks: {missing}"


def test_every_variable_of_the_scripts_is_documented():
    names: set[str] = set()
    for script in (ROOT / "scripts").rglob("*"):
        if script.is_file() and script.suffix in {".sh", ".py"}:
            text = script.read_text(encoding="utf-8")
            names |= set(re.findall(r"\$\{((?:KS|AB|RT)_[A-Z_]+)[:}-]", text))
            names |= set(re.findall(r"""environ\.get\(\s*["']((?:YTP|RT)_[A-Z_]+)["']""", text))
    names |= {"YTP_NETWORK_TESTS", "YTP_YTDLP_PROXY", "YTP_VPN_ENV_FILE", "YTP_OVPN_SOURCE"}
    assert {"KS_COMPOSE", "AB_PROBE_URL", "YTP_BACKUP_DIR", "RT_COMPOSE", "RT_IP_URL"} <= names, (
        "the scan itself works"
    )
    missing = sorted(n for n in names if n not in CONFIGURATION)
    assert not missing, f"docs/configuration.md lacks: {missing}"


def test_the_link_scan_finds_links():
    assert len(links(ROOT / "README.md")) >= 4
    assert "5c-prove-the-killswitch-issue-4" in anchors(ROOT / "docs" / "host-setup.md")
