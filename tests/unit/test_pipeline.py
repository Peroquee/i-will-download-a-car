"""Structural checks on .gitlab-ci.yml, so pipeline edits can't silently drop a test level."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
RESERVED = {
    "workflow",
    "stages",
    "variables",
    "default",
    "include",
    "image",
    "services",
    "cache",
    "before_script",
    "after_script",
}
JOBS: dict[str, dict[str, Any]] = {
    name: body
    for name, body in CI.items()
    if name not in RESERVED and not name.startswith(".") and isinstance(body, dict)
}
LEVELS = ("unit", "integration", "smoke", "system", "regression")


def resolved(job: dict[str, Any]) -> dict[str, Any]:
    """Apply `extends` (in order, later wins) like GitLab does, one level of merging."""
    merged: dict[str, Any] = {}
    parents = job.get("extends", [])
    for parent in [parents] if isinstance(parents, str) else parents:
        merged.update(resolved(CI[parent]))
    merged.update({k: v for k, v in job.items() if k != "extends"})
    return merged


def script_text(job: dict[str, Any]) -> str:
    def flat(items: Any) -> list[str]:
        if isinstance(items, str):
            return [items]
        return [s for item in items or [] for s in flat(item)]

    return "\n".join(flat(job.get("script")))


def test_stages_are_the_test_levels_in_order():
    assert CI["stages"] == ["lint", *LEVELS]


@pytest.mark.parametrize("level", LEVELS)
def test_every_test_level_has_exactly_one_job_running_its_folder(level):
    runners = [n for n, j in JOBS.items() if f"pytest tests/{level}" in script_text(resolved(j))]
    assert runners == [f"test:{level}"]
    assert resolved(JOBS[f"test:{level}"])["stage"] == level


@pytest.mark.parametrize("name", sorted(JOBS))
def test_job_stage_exists(name):
    assert resolved(JOBS[name])["stage"] in CI["stages"]


@pytest.mark.parametrize("name", sorted(JOBS))
def test_needs_reference_real_jobs(name):
    for need in resolved(JOBS[name]).get("needs", []):
        target = need if isinstance(need, str) else need["job"]
        assert target in JOBS, f"{name} needs unknown job {target}"


def test_only_daemon_jobs_are_tagged_linux():
    tagged = {n for n, j in JOBS.items() if "linux" in resolved(j).get("tags", [])}
    assert tagged == {"test:smoke", "test:system", "test:regression"}


def test_daemon_jobs_fail_instead_of_skipping_missing_capabilities():
    for name in ("test:smoke", "test:system", "test:regression"):
        assert resolved(JOBS[name])["variables"]["REQUIRE_CAPABILITIES"] == "1"


@pytest.mark.parametrize("name", [n for n in JOBS if n.startswith("test:")])
def test_test_jobs_publish_junit_reports(name):
    job = resolved(JOBS[name])
    assert "$PYTEST_COMMON" in script_text(job)
    assert job["artifacts"]["reports"]["junit"] == "reports/junit-*.xml"
    assert job["artifacts"]["when"] == "always"


def test_junit_path_is_per_job():
    assert "--junitxml=reports/junit-$CI_JOB_NAME_SLUG.xml" in CI["variables"]["PYTEST_COMMON"]


@pytest.mark.security
def test_secret_scan_covers_full_history():
    job = JOBS["lint:secrets"]
    assert job["variables"]["GIT_DEPTH"] == 0
    assert "gitleaks git" in script_text(job)


def test_lint_covers_python_yaml_and_shell():
    text = "\n".join(script_text(resolved(j)) for n, j in JOBS.items() if n.startswith("lint:"))
    for tool in ("ruff check", "ruff format --check", "yamllint", "shellcheck"):
        assert tool in text
