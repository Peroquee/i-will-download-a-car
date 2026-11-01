"""install.sh and update.sh: the thin bash wrappers around the installer (issue #37)."""

# ruff: noqa: E501

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEBIAN = "ID=debian\nVERSION_CODENAME=bookworm\n"


def fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def sandbox(tmp_path):
    """A project folder with a copy of the wrappers, and fake python3/sudo/git that only record."""
    project = tmp_path / "project"
    (project / "scripts" / "install").mkdir(parents=True)
    (project / "scripts" / "install" / "install.py").write_text("# stub\n")
    for name in ("install.sh", "update.sh"):
        shutil.copy(ROOT / name, project / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    fake(bin_dir, "python3", f'echo "python3 $*" >> {calls}')
    fake(bin_dir, "sudo", f'echo "sudo $*" >> {calls}')
    fake(
        bin_dir,
        "git",
        f'echo "git $*" >> {calls}; mkdir -p youtube-premium; cp {ROOT}/install.sh youtube-premium/install.sh; mkdir -p youtube-premium/scripts/install; touch youtube-premium/scripts/install/install.py',
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(DEBIAN)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OS_RELEASE": str(os_release),
        "HOME": str(tmp_path),
    }
    return project, env, calls, os_release


def run(project: Path, env: dict, *args: str, script: str = "install.sh", cwd: Path | None = None):
    return subprocess.run(
        ["bash", str(project / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or project,
        check=False,
        timeout=30,
    )


def test_help_needs_no_system_checks(sandbox):
    project, env, _, os_release = sandbox
    os_release.write_text("ID=other\n")
    result = run(project, env, "--help")
    assert result.returncode == 0
    assert "./install.sh --update" in result.stdout
    assert "--dry-run" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        "ID=ubuntu\nVERSION_CODENAME=jammy\n",
        "ID=debian\nVERSION_CODENAME=trixie\n",
        "ID=debian\nVERSION_CODENAME=bullseye\n",
        "",
    ],
)
def test_only_debian_12_is_accepted(sandbox, content):
    project, env, calls, os_release = sandbox
    os_release.write_text(content)
    result = run(project, env, "--dry-run")
    assert result.returncode == 1
    assert "Debian 12" in result.stderr
    assert "Nur Debian 12" in result.stderr
    assert not calls.exists(), "nothing was started"


def test_a_missing_os_release_is_refused_too(sandbox):
    project, env, calls, os_release = sandbox
    os_release.unlink()
    assert run(project, env, "--dry-run").returncode == 1
    assert not calls.exists()


def test_a_dry_run_needs_no_root_and_passes_every_argument_on(sandbox):
    project, env, calls, _ = sandbox
    result = run(project, env, "--lang", "de", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert (
        calls.read_text().strip()
        == f"python3 {project}/scripts/install/install.py --lang de --dry-run"
    )


def test_without_root_it_starts_itself_again_through_sudo(sandbox):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    project, env, calls, _ = sandbox
    result = run(project, env, "--lang", "en")
    assert result.returncode == 0, result.stderr
    assert "sudo -E bash" in calls.read_text()
    assert "--lang en" in calls.read_text()
    assert "sudo" in result.stdout.lower()


def test_without_root_and_without_sudo_it_explains(sandbox, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    project, env, _, _ = sandbox
    tools = tmp_path / "tools"  # only what the script needs: no sudo among it
    tools.mkdir()
    for name in ("bash", "dirname", "id"):
        (tools / name).symlink_to(shutil.which(name) or f"/usr/bin/{name}")
    (Path(env["PATH"].split(":")[0]) / "sudo").unlink()
    result = run(project, {**env, "PATH": f"{tools}:{Path(env['PATH'].split(':')[0])}"})
    assert result.returncode == 1
    assert "su -" in result.stderr


def test_python_is_installed_when_it_is_missing(sandbox, tmp_path):
    if os.geteuid() != 0:
        pytest.skip("needs root to reach the apt step")
    project, env, calls, _ = sandbox
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "python3").unlink()
    fake(bin_dir, "apt-get", f'echo "apt-get $*" >> {calls}')
    tools = tmp_path / "tools"  # only what the script needs, so no real python3 is found
    tools.mkdir()
    for name in ("bash", "dirname", "id"):
        (tools / name).symlink_to(shutil.which(name) or f"/usr/bin/{name}")
    result = run(project, {**env, "PATH": f"{bin_dir}:{tools}"})
    assert "apt-get install -y python3" in calls.read_text(), result.stderr


def test_a_dry_run_without_python_does_not_install_anything(sandbox):
    project, env, _, _ = sandbox
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "python3").unlink()
    only_basics = {**env, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    if shutil.which("python3", path="/usr/bin:/bin"):
        pytest.skip("this machine has a python3 in /usr/bin")
    result = run(project, only_basics, "--dry-run")
    assert result.returncode == 1
    assert "python3" in result.stderr


def test_a_lone_install_sh_downloads_the_project_first(sandbox, tmp_path):
    _, env, calls, _ = sandbox
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(ROOT / "install.sh", lone / "install.sh")
    result = run(lone, env, "--dry-run", cwd=lone)
    clone = [line for line in calls.read_text().splitlines() if line.startswith("git clone")]
    assert clone == [
        "git clone https://github.com/Peroquee/i-will-download-a-car.git youtube-premium"
    ], result.stderr


def test_the_download_address_is_one_setting(sandbox, tmp_path):
    _, env, calls, _ = sandbox
    lone = tmp_path / "lone2"
    lone.mkdir()
    shutil.copy(ROOT / "install.sh", lone / "install.sh")
    run(lone, {**env, "YTP_REPO_URL": "https://example.invalid/x.git"}, "--dry-run", cwd=lone)
    assert "git clone https://example.invalid/x.git youtube-premium" in calls.read_text()


def test_an_existing_folder_is_not_overwritten_by_the_download(sandbox, tmp_path):
    _, env, calls, _ = sandbox
    lone = tmp_path / "lone3"
    (lone / "youtube-premium").mkdir(parents=True)
    shutil.copy(ROOT / "install.sh", lone / "install.sh")
    result = run(lone, env, "--dry-run", cwd=lone)
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert not calls.exists()


def test_update_sh_is_install_sh_update(sandbox):
    project, env, calls, _ = sandbox
    result = run(project, env, "--dry-run", script="update.sh")
    assert result.returncode == 0, result.stderr
    assert calls.read_text().strip().endswith("install.py --update --dry-run")


def test_the_wrappers_follow_the_script_conventions():
    for name in ("install.sh", "update.sh"):
        path = ROOT / name
        text = path.read_text()
        assert text.startswith("#!/usr/bin/env bash\n")
        assert text.splitlines()[1].startswith(f"# {name}:")
        assert "set -euo pipefail" in text
        assert os.access(path, os.X_OK)
        assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0


def test_shellcheck_is_clean_if_it_is_installed():
    tool = shutil.which("shellcheck")
    if tool is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [tool, "-x", "--severity=style", str(ROOT / "install.sh"), str(ROOT / "update.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_the_default_download_address_is_the_public_repository():
    text = (ROOT / "install.sh").read_text()
    assert "YTP_REPO_URL:-https://github.com/Peroquee/i-will-download-a-car.git" in text
    assert text.count("github.com/Peroquee") == 1, "one constant, nowhere else in the script"
