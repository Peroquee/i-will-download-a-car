"""scripts/host/bootstrap.sh in --dry-run mode: argument rules, OS guard, planned commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, StubBin, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host" / "bootstrap.sh"

BOOKWORM = (
    'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
    'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
)
TRIXIE = 'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
UBUNTU = "ID=ubuntu\nVERSION_CODENAME=noble\n"


@pytest.fixture
def env(tmp_path: Path):
    stubs = StubBin(tmp_path / "stubs")
    stubs.path.mkdir()
    # Nothing exists yet on the fake host: no media group/user, empty device.
    stubs.add("getent", "exit 2")
    stubs.add("blkid", "exit 2")
    stubs.add("dpkg", "echo amd64")
    os_release = tmp_path / "os-release"
    os_release.write_text(BOOKWORM)
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=1111 / ext4 errors=remount-ro 0 1\n")
    return {"stubs": stubs, "os_release": os_release, "fstab": fstab, "tmp": tmp_path}


def bootstrap(env, *args: str, dry_run: bool = True, qga_device: bool = False) -> ScriptResult:
    """Run bootstrap.sh; qga_device says whether the fake host has the guest-agent device."""
    argv = list(args) + (["--dry-run"] if dry_run else [])
    device = env["tmp"] / "org.qemu.guest_agent.0"
    if qga_device:
        device.touch()
    return run_script(
        SCRIPT,
        argv,
        stubs=env["stubs"],
        env={
            "OS_RELEASE": str(env["os_release"]),
            "FSTAB_FILE": str(env["fstab"]),
            "QGA_DEVICE": str(device),
        },
    )


def planned(result: ScriptResult) -> list[str]:
    return result.lines_starting("+ ")


# --------------------------------------------------------------------------- arguments


def test_user_is_required(env):
    result = bootstrap(env)
    assert result.returncode != 0
    assert "--user is required" in result.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--user", "u", "--puid", "abc"], "numeric"),
        (["--user", "u", "--pgid", "0"], "must not be 0"),
        (["--user", "u", "--data-root", "data"], "absolute"),
        (["--user", "u", "--media-device", "/dev/sdb", "--media-uuid", "x"], "either"),
        (["--user", "u", "--format-ext4"], "needs --media-device"),
        (["--user", "u", "--wat"], "unknown argument"),
    ],
)
def test_invalid_arguments_are_rejected(env, args, message):
    result = bootstrap(env, *args)
    assert result.returncode != 0
    assert message in result.stderr


def test_help(env):
    result = run_script(SCRIPT, ["--help"])
    assert result.returncode == 0
    assert "--media-device" in result.stdout


# --------------------------------------------------------------------------- preconditions


@pytest.mark.parametrize(("os_release", "found"), [(TRIXIE, "trixie"), (UBUNTU, "ubuntu")])
def test_refuses_anything_but_debian_12(env, os_release, found):
    env["os_release"].write_text(os_release)
    result = bootstrap(env, "--user", "u")
    assert result.returncode != 0
    assert "expected Debian 12" in result.stderr
    assert found in result.stderr


def test_refuses_to_run_without_root_unless_dry_run(env):
    env["stubs"].add("id", "echo 1000")
    result = bootstrap(env, "--user", "u", dry_run=False)
    assert result.returncode != 0
    assert "must run as root" in result.stderr


def test_dry_run_changes_nothing(env):
    before = env["fstab"].read_text()
    result = bootstrap(env, "--user", "u", "--media-uuid", "abcd-1234")
    assert result.returncode == 0, result.stderr
    assert env["fstab"].read_text() == before


# --------------------------------------------------------------------------- docker


def test_installs_docker_from_the_official_repo(env):
    result = bootstrap(env, "--user", "alice")
    lines = "\n".join(planned(result))
    assert "download.docker.com/linux/debian bookworm stable" in lines
    assert "signed-by=/etc/apt/keyrings/docker.asc" in lines
    install = next(line for line in planned(result) if "docker-ce " in line)
    for package in (
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ):
        assert package in install.split()


def test_does_not_use_debian_docker_io_package(env):
    # docker.io from Debian lags behind and has no compose plugin; never mix the two.
    assert not any("docker.io" in line.split() for line in planned(bootstrap(env, "--user", "u")))


def test_enables_docker_and_adds_user_to_group(env):
    lines = planned(bootstrap(env, "--user", "alice"))
    assert "+ systemctl enable --now docker" in lines
    assert "+ usermod -aG docker alice" in lines


def test_guest_agent_is_enabled_but_never_started_blocking(env):
    """Regression: `enable --now qemu-guest-agent` waited ~90 s for a missing virtio device,
    failed, and set -e aborted bootstrap before the tun and /data steps."""
    for qga_device in (False, True):
        lines = planned(bootstrap(env, "--user", "u", qga_device=qga_device))
        assert "+ systemctl enable qemu-guest-agent" in lines
        assert not any("qemu-guest-agent" in line and "--now" in line.split() for line in lines), (
            lines
        )
        starts = [ln for ln in lines if "systemctl start" in ln and "qemu-guest-agent" in ln]
        assert all("--no-block" in ln.split() for ln in starts), starts


def test_guest_agent_is_started_only_when_its_device_exists(env):
    lines = planned(bootstrap(env, "--user", "u", qga_device=True))
    assert "+ systemctl start --no-block qemu-guest-agent" in lines


def test_without_the_guest_agent_device_it_is_not_started_and_a_hint_is_logged(env):
    result = bootstrap(env, "--user", "u", qga_device=False)
    assert result.returncode == 0, result.stderr
    assert not any("systemctl start" in line for line in planned(result))
    assert "QEMU Guest Agent" in result.stdout
    assert "Options" in result.stdout


def test_the_tun_and_media_steps_still_follow_the_guest_agent_step(env):
    lines = planned(bootstrap(env, "--user", "u", qga_device=False))
    docker = lines.index("+ systemctl enable --now docker")
    assert docker < lines.index("+ write /etc/modules-load.d/tun.conf: tun")


def test_tun_module_loads_on_boot(env):
    assert "+ write /etc/modules-load.d/tun.conf: tun" in planned(bootstrap(env, "--user", "u"))


# --------------------------------------------------------------------------- media owner


def test_creates_media_owner_when_missing(env):
    lines = planned(bootstrap(env, "--user", "u", "--puid", "1500", "--pgid", "1600"))
    assert "+ groupadd --gid 1600 media" in lines
    assert (
        "+ useradd --uid 1500 --gid 1600 --no-create-home --shell /usr/sbin/nologin media" in lines
    )


def test_reuses_existing_media_owner(env):
    env["stubs"].add("getent", "echo exists; exit 0")
    lines = planned(bootstrap(env, "--user", "u"))
    assert not any(line.startswith(("+ groupadd", "+ useradd")) for line in lines)


# --------------------------------------------------------------------------- media volume


def test_without_volume_creates_data_root_on_system_disk(env):
    result = bootstrap(env, "--user", "u", "--data-root", "/srv/data")
    assert "+ install -d -m 2775 -o 1000 -g 1000 /srv/data" in planned(result)
    assert "attach a volume before real use" in result.stdout


def test_media_uuid_goes_into_fstab_with_nofail(env):
    lines = planned(bootstrap(env, "--user", "u", "--media-uuid", "abcd-1234"))
    entry = next(line for line in lines if line.startswith(f"+ append to {env['fstab']}"))
    assert "UUID=abcd-1234 /data ext4" in entry
    assert "nofail" in entry
    assert "+ mount /data" in lines
    assert "+ chown 1000:1000 /data" in lines
    assert "+ chmod 2775 /data" in lines


def test_fstab_entry_is_not_duplicated(env):
    entry = "UUID=abcd-1234 /data ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2"
    env["fstab"].write_text(env["fstab"].read_text() + entry + "\n")
    result = bootstrap(env, "--user", "u", "--media-uuid", "abcd-1234")
    assert not any(line.startswith("+ append to") for line in planned(result))
    assert "already present" in result.stdout


def test_media_device_uuid_is_resolved(env):
    env["stubs"].add(
        "blkid",
        """
case "$*" in
  *"-s TYPE"*) echo ext4 ;;
  *"-s UUID"*) echo dead-beef ;;
esac""",
    )
    lines = planned(bootstrap(env, "--user", "u", "--media-device", "/dev/sdb"))
    assert any("UUID=dead-beef /data ext4" in line for line in lines)
    assert not any("mkfs" in line for line in lines)


def test_empty_device_requires_explicit_format_flag(env):
    result = bootstrap(env, "--user", "u", "--media-device", "/dev/sdb")
    assert result.returncode != 0
    assert "--format-ext4" in result.stderr


def test_empty_device_is_formatted_with_flag(env):
    lines = planned(bootstrap(env, "--user", "u", "--media-device", "/dev/sdb", "--format-ext4"))
    assert "+ mkfs.ext4 -L media /dev/sdb" in lines


# Never formatting a device that has a filesystem is pinned in
# tests/regression/test_c0_host_decisions.py.
