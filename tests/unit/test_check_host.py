"""scripts/host/check-host.sh, tested against stub commands and a fake filesystem root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.shell import ScriptResult, StubBin, isolated_bin, run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host" / "check-host.sh"
REAL_TUN = Path("/dev/net/tun")


# --------------------------------------------------------------------------- fixtures


@dataclass
class Host:
    """A fake host: stub commands, fake root, env file."""

    tmp: Path
    stubs: StubBin
    base: Path
    root: Path
    env_file: Path
    puid: int
    pgid: int

    @property
    def app_config(self) -> Path:
        """The fake config/app: where the app keeps its database."""
        return self.tmp / "config-app"

    def docker(
        self,
        *,
        server: str = "27.3.1",
        compose: str = "2.29.7",
        info_rc: int = 0,
        compose_rc: int = 0,
    ) -> None:
        self.stubs.add(
            "docker",
            f"""
case "$1" in
  info) [ {info_rc} -eq 0 ] || exit {info_rc}; echo "{server}" ;;
  compose) [ {compose_rc} -eq 0 ] || exit {compose_rc}; echo "{compose}" ;;
  *) exit 99 ;;
esac""",
        )

    def virt(self, *, container: str = "none", vm: str = "kvm") -> None:
        self.stubs.add(
            "systemd-detect-virt",
            f"""
if [ "$1" = "--container" ]; then echo "{container}"; [ "{container}" != none ] || exit 1
else echo "{vm}"; [ "{vm}" != none ] || exit 1; fi""",
        )

    def mounted(self, yes: bool = True) -> None:
        self.stubs.add("mountpoint", "exit 0" if yes else "exit 1")

    def tun(self, kind: str) -> None:
        """kind: real | missing | regular | wrong-device"""
        target = self.root / "dev" / "net" / "tun"
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "real":
            target.symlink_to(REAL_TUN)
        elif kind == "regular":
            target.write_text("")
        elif kind == "wrong-device":
            target.symlink_to("/dev/null")  # a char device, but 1:3 instead of 10:200

    def data_dir(self, *, mode: int = 0o2775, owner: tuple[int, int] | None = None) -> Path:
        path = self.root / "data"
        path.mkdir(exist_ok=True)
        path.chmod(mode)
        if owner is not None:
            os.chown(path, *owner)
        return path

    def layout(self, *, skip: tuple[str, ...] = ()) -> None:
        """The download and library folders under the fake /data (minus the ones in skip)."""
        data = self.root / "data"
        data.mkdir(exist_ok=True)
        for rel in (
            "downloads/movies",
            "downloads/series",
            "downloads/audiobooks",
            "downloads/music",
            "Filme",
            "Serien",
            "Hörbücher",
            "Musik",
            "Podcasts",
            "Youtube",
        ):
            if rel not in skip:
                (data / rel).mkdir(parents=True, exist_ok=True)
        if BACKUPS not in skip:
            backups = data / BACKUPS
            backups.mkdir(exist_ok=True)
            backups.chmod(0o700)
            if os.geteuid() == 0:
                os.chown(backups, self.puid, self.pgid)

    def write_env(self, **overrides: str | None) -> None:
        values: dict[str, str | None] = {
            "DATA_ROOT": "/data",
            "PUID": str(self.puid),
            "PGID": str(self.pgid),
            "UMASK": "002",
            "MEDIA_DIR_MOVIES": "Filme",
            "MEDIA_DIR_SERIES": "Serien",
            "MEDIA_DIR_AUDIOBOOKS": "Hörbücher",
            "MEDIA_DIR_MUSIC": "Musik",
            "MEDIA_DIR_PODCASTS": "Podcasts",
            "MEDIA_DIR_YOUTUBE": "Youtube",
        }
        values.update(overrides)
        self.env_file.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
        )

    def run(self, *args: str) -> ScriptResult:
        return run_script(
            SCRIPT,
            ["--env-file", str(self.env_file), *args],
            stubs=self.stubs,
            isolate_path=self.base,
            env={"CHECK_ROOT": str(self.root), "APP_CONFIG_DIR": str(self.app_config)},
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    # As root we can chown to a non-root owner; otherwise our own uid is non-root already.
    if os.geteuid() == 0:
        puid = pgid = 4242
    else:
        puid, pgid = os.getuid(), os.getgid()
    h = Host(
        tmp_path,
        StubBin(stubs_dir),
        isolated_bin(tmp_path),
        root,
        tmp_path / "host.env",
        puid,
        pgid,
    )
    h.write_env()
    # A correct app state folder, as create-layout.sh leaves it; tests break it on purpose.
    h.app_config.mkdir()
    h.app_config.chmod(0o2775)
    if os.geteuid() == 0:
        os.chown(h.app_config, puid, pgid)
    return h


BACKUPS = ".youtube-premium-backups"


def owner_of(h: Host) -> tuple[int, int] | None:
    return (h.puid, h.pgid) if os.geteuid() == 0 else None


def status(result: ScriptResult, check: str) -> str:
    """PASS/FAIL/SKIP for one check, from the script's output."""
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[1] == check:
            return parts[0]
    raise AssertionError(f"no result line for {check!r} in:\n{result.stdout}")


# --------------------------------------------------------------------------- whole run


@pytest.mark.skipif(not REAL_TUN.exists(), reason="needs a real /dev/net/tun to symlink to")
def test_healthy_host_passes_everything(host):
    host.docker()
    host.virt()
    host.mounted()
    host.tun("real")
    host.data_dir(owner=owner_of(host))
    host.layout()
    result = host.run()
    assert result.returncode == 0, result.stdout + result.stderr
    checks = ("virt", "docker", "compose", "tun", "media", "layout", "backups")
    assert [status(result, c) for c in checks] == ["PASS"] * 7
    assert result.stdout.rstrip().endswith("OK: all checks passed")


def test_failure_count_and_exit_code(host):
    # docker missing (not stubbed, isolated PATH) and tun missing: two failures.
    result = host.run("--only", "docker,tun")
    assert result.returncode == 1
    assert "FAILED: 2 check(s) failed" in result.stdout


def test_only_runs_the_selected_checks(host):
    host.docker()
    result = host.run("--only", "docker")
    checks = [
        line.split()[1]
        for line in result.stdout.splitlines()
        if line[:4] in {"PASS", "FAIL", "SKIP"}
    ]
    assert checks == ["docker"]


@pytest.mark.parametrize("args", [["--only", "nonsense"], ["--bogus"], ["--only"], ["--env-file"]])
def test_bad_usage_exits_2(host, args):
    result = run_script(SCRIPT, args, isolate_path=host.base)
    assert result.returncode == 2


def test_a_check_without_a_dispatch_arm_fails_loudly(host):
    """A check added to ALL_CHECKS but not to main()'s case must not be skipped silently."""
    source = SCRIPT.read_text(encoding="utf-8")
    patched = source.replace(
        'readonly ALL_CHECKS="virt docker compose tun media layout backups"',
        'readonly ALL_CHECKS="virt docker compose tun media layout backups orphan"',
    )
    assert patched != source, "ALL_CHECKS line changed; update this test"
    script = host.tmp / "check-host-orphan.sh"
    script.write_text(patched, encoding="utf-8")
    result = run_script(script, ["--only", "orphan"], isolate_path=host.base)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no dispatch for check: orphan" in result.stderr
    assert "OK" not in result.stdout


def test_help_exits_0_and_documents_the_checks(host):
    result = run_script(SCRIPT, ["--help"], isolate_path=host.base)
    assert result.returncode == 0
    assert "virt,docker,compose,tun,media,layout" in result.stdout


# --------------------------------------------------------------------------- virt


def test_virt_passes_in_a_vm(host):
    host.virt(vm="kvm")
    result = host.run("--only", "virt")
    assert status(result, "virt") == "PASS"
    assert "kvm" in result.stdout


@pytest.mark.parametrize("container", ["lxc", "docker", "podman"])
def test_virt_fails_inside_a_container(host, container):
    host.virt(container=container)
    result = host.run("--only", "virt")
    assert status(result, "virt") == "FAIL"
    assert container in result.stdout
    assert result.returncode == 1


def test_virt_passes_on_bare_metal_with_note(host):
    host.virt(vm="none")
    result = host.run("--only", "virt")
    assert status(result, "virt") == "PASS"
    assert "bare metal" in result.stdout


def test_virt_skips_without_systemd_detect_virt(host):
    result = host.run("--only", "virt")
    assert status(result, "virt") == "SKIP"
    assert result.returncode == 0


# --------------------------------------------------------------------------- docker / compose


def test_docker_cli_missing(host):
    result = host.run("--only", "docker")
    assert status(result, "docker") == "FAIL"
    assert "not installed" in result.stdout


def test_docker_daemon_unreachable(host):
    host.docker(info_rc=1)
    result = host.run("--only", "docker")
    assert status(result, "docker") == "FAIL"
    assert "daemon not reachable" in result.stdout


def test_docker_reports_server_version(host):
    host.docker(server="28.0.1")
    result = host.run("--only", "docker")
    assert status(result, "docker") == "PASS"
    assert "28.0.1" in result.stdout


def test_compose_plugin_missing(host):
    host.docker(compose_rc=1)
    result = host.run("--only", "compose")
    assert status(result, "compose") == "FAIL"
    assert "Compose plugin not installed" in result.stdout


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.16.0", "FAIL"),
        ("1.29.2", "FAIL"),
        ("2.17.0", "PASS"),
        ("2.29.7", "PASS"),
        ("v2.30.1", "PASS"),
        ("5.6.1", "PASS"),
        ("garbage", "FAIL"),
    ],
)
def test_compose_minimum_version(host, version, expected):
    host.docker(compose=version)
    assert status(host.run("--only", "compose"), "compose") == expected


# --------------------------------------------------------------------------- tun


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "missing"),
        ("regular", "not a character device"),
        ("wrong-device", "expected 10:200"),
    ],
)
def test_tun_failures(host, kind, message):
    host.tun(kind)
    result = host.run("--only", "tun")
    assert status(result, "tun") == "FAIL"
    assert message in result.stdout


@pytest.mark.skipif(not REAL_TUN.exists(), reason="needs a real /dev/net/tun")
def test_tun_passes_for_the_real_device(host):
    host.tun("real")
    assert status(host.run("--only", "tun"), "tun") == "PASS"


# --------------------------------------------------------------------------- media


def _media(host: Host) -> ScriptResult:
    return host.run("--only", "media")


def test_media_passes(host):
    host.mounted()
    host.data_dir(owner=owner_of(host))
    result = _media(host)
    assert status(result, "media") == "PASS", result.stdout
    assert "2775" in result.stdout


def test_media_env_file_missing(host):
    host.env_file.unlink()
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert "not found" in result.stdout


@pytest.mark.parametrize("missing", ["DATA_ROOT", "PUID", "PGID", "UMASK"])
def test_media_required_variable_missing(host, missing):
    host.write_env(**{missing: None})
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert missing in result.stdout


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"PUID": "abc"}, "numeric"),
        ({"PGID": "1o00"}, "numeric"),
        ({"PUID": "0"}, "must not be root"),
        ({"PGID": "0"}, "must not be root"),
        ({"UMASK": "999"}, "octal"),
        ({"UMASK": "22"}, "octal"),
    ],
)
def test_media_invalid_values(host, overrides, message):
    host.write_env(**overrides)
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert message in result.stdout


def test_media_quoted_values_are_accepted(host):
    host.mounted()
    host.data_dir(owner=owner_of(host))
    host.write_env(DATA_ROOT='"/data"', UMASK="'002'")
    assert status(_media(host), "media") == "PASS"


def test_media_dir_missing(host):
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert "does not exist" in result.stdout


def test_media_not_a_mount_point(host):
    host.mounted(False)
    host.data_dir(owner=owner_of(host))
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert "not a mount point" in result.stdout


def test_media_wrong_owner(host):
    host.mounted()
    host.data_dir(owner=owner_of(host))
    host.write_env(PUID=str(host.puid + 1))
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert "owned by" in result.stdout


def test_media_not_group_writable(host):
    host.mounted()
    host.data_dir(mode=0o755, owner=owner_of(host))
    result = _media(host)
    assert status(result, "media") == "FAIL"
    assert "not group-writable" in result.stdout


# --------------------------------------------------------------------------- layout


def _layout(host: Host) -> ScriptResult:
    return host.run("--only", "layout")


def test_layout_passes_and_proves_a_hardlink(host):
    host.layout()
    result = _layout(host)
    assert status(result, "layout") == "PASS", result.stdout
    assert "link count 2" in result.stdout
    assert "Hörbücher" in result.stdout  # non-ASCII folder names work end to end


def test_layout_leaves_no_temp_files_behind(host):
    host.layout()
    _layout(host)
    assert not list((host.root / "data").rglob(".ytp-linktest*"))


def test_layout_never_touches_existing_files(host):
    host.layout()
    movie = host.root / "data" / "Filme" / "Film (2020)" / "film.mkv"
    movie.parent.mkdir()
    movie.write_text("x")
    _layout(host)
    assert movie.read_text() == "x"
    assert os.stat(movie).st_nlink == 1


def test_layout_fails_with_a_hint_when_folders_are_missing(host):
    host.layout(skip=("downloads/music", "Youtube"))
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert "downloads/music" in result.stdout
    assert "Youtube" in result.stdout
    assert "create-layout.sh" in result.stdout
    assert result.returncode == 1


@pytest.mark.parametrize("missing", ["MEDIA_DIR_MOVIES", "MEDIA_DIR_YOUTUBE", "DATA_ROOT"])
def test_layout_fails_when_the_env_file_lacks_a_setting(host, missing):
    host.layout()
    host.write_env(**{missing: None})
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert missing in result.stdout


def test_layout_fails_without_an_env_file(host):
    host.env_file.unlink()
    assert status(_layout(host), "layout") == "FAIL"


def test_layout_fails_on_different_filesystems(host):
    host.layout()
    # Pretend the library folder Filme sits on another device than downloads/.
    host.stubs.add(
        "stat",
        'case "$*" in *"-c %d"*Filme*) echo 99 ;; *) exec /usr/bin/stat "$@" ;; esac',
    )
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert "different filesystem" in result.stdout


def test_layout_reports_a_failing_hardlink_and_cleans_up(host):
    host.layout()
    host.stubs.add("ln", 'echo "Operation not permitted" >&2; exit 1')
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert "hardlink downloads/movies -> Filme failed" in result.stdout
    assert "Operation not permitted" in result.stdout
    assert not list((host.root / "data").rglob(".ytp-linktest*"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_layout_without_write_access_is_skipped_with_a_hint_not_failed(host):
    """The login user is not in the media group, so it cannot write the link-test file.
    The folders are fine, so this must not be a FAIL; it says what to do instead."""
    host.layout()
    folder = host.root / "data" / "downloads" / "movies"
    folder.chmod(0o555)
    try:
        result = _layout(host)
    finally:
        folder.chmod(0o755)
    assert status(result, "layout") == "SKIP"
    assert result.returncode == 0
    assert "NOT tested" in result.stdout
    assert "sudo scripts/host/check-host.sh --only layout" in result.stdout
    assert "usermod -aG media" in result.stdout
    assert not list((host.root / "data").rglob(".ytp-linktest*"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_layout_without_write_access_still_fails_when_folders_are_missing(host):
    host.layout(skip=("Youtube",))
    (host.root / "data" / "downloads" / "movies").chmod(0o555)
    try:
        result = _layout(host)
    finally:
        (host.root / "data" / "downloads" / "movies").chmod(0o755)
    assert status(result, "layout") == "FAIL"
    assert "Youtube" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_layout_runs_the_link_test_as_the_media_owner_when_sudo_allows_it(host):
    host.layout()
    folder = host.root / "data" / "downloads" / "movies"
    # A stand-in for `sudo -n -u '#UID' -g '#GID' CMD...` that is allowed to write.
    host.stubs.add(
        "sudo",
        f"""
echo "sudo $*" >> "{host.tmp}/sudo.log"
[ "$1" = "-n" ] && shift
[ "$1" = "-u" ] && shift 2
[ "$1" = "-g" ] && shift 2
chmod 755 "{folder}"
exec "$@"
""",
    )
    folder.chmod(0o555)
    try:
        result = _layout(host)
    finally:
        folder.chmod(0o755)
    assert status(result, "layout") == "PASS", result.stdout
    assert f"run as {host.puid}:{host.pgid} via sudo" in result.stdout
    log = (host.tmp / "sudo.log").read_text()
    assert f"-u #{host.puid} -g #{host.pgid}" in log
    assert not list((host.root / "data").rglob(".ytp-linktest*"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_layout_skips_when_sudo_would_need_a_password(host):
    host.layout()
    host.stubs.add("sudo", "exit 1")  # `sudo -n` refuses: a password would be needed
    folder = host.root / "data" / "downloads" / "movies"
    folder.chmod(0o555)
    try:
        result = _layout(host)
    finally:
        folder.chmod(0o755)
    assert status(result, "layout") == "SKIP"


# ------------------------------------------------------------------ layout: app state folder


def test_layout_fails_when_the_app_state_folder_is_missing(host):
    host.layout()
    host.app_config.rmdir()
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert "state folder" in result.stdout
    assert "create-layout.sh" in result.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to give the folder to another user")
def test_layout_fails_when_the_app_state_folder_belongs_to_root(host):
    """What Docker leaves behind for a missing bind-mount source: root:root, app cannot write."""
    host.layout()
    os.chown(host.app_config, 0, 0)
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert f"owned by 0:0, but the app runs as {host.puid}:{host.pgid}" in result.stdout
    assert "create-layout.sh" in result.stdout


def test_layout_fails_when_the_owner_cannot_use_the_state_folder(host):
    host.layout()
    host.app_config.chmod(0o555)
    result = _layout(host)
    assert status(result, "layout") == "FAIL"
    assert "needs rwx" in result.stdout


def test_layout_passes_with_a_correct_app_state_folder(host):
    host.layout()
    assert status(_layout(host), "layout") == "PASS"


# --- the backup folder (issue #41) --------------------------------------------------------------


def backups_host(host, **kw):
    host.docker()
    host.virt()
    host.mounted()
    host.tun("real")
    host.data_dir(owner=owner_of(host))
    host.layout(**kw)
    return host


def test_the_backup_folder_passes_when_private_and_on_the_media_drive(host):
    backups_host(host)
    result = host.run("--only", "backups")
    assert result.returncode == 0, result.stdout
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS backups"))
    assert "mode 700" in line
    assert "on the media drive" in line


def test_a_missing_backup_folder_says_how_to_create_it(host):
    backups_host(host, skip=(BACKUPS,))
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "create-layout.sh" in result.stdout


@pytest.mark.security
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o777, 0o770])
def test_a_backup_folder_others_can_read_fails(host, mode):
    backups_host(host)
    (host.root / "data" / BACKUPS).chmod(mode)
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "chmod 700" in result.stdout


@pytest.mark.security
def test_an_archive_others_can_read_fails(host):
    backups_host(host)
    archive = host.root / "data" / BACKUPS / "ytp-backup-20261102T090000Z.tar.gz"
    archive.write_text("x")
    archive.chmod(0o644)
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "chmod 600" in result.stdout
    archive.chmod(0o600)
    assert status(host.run("--only", "backups"), "backups") == "PASS"


@pytest.mark.security
def test_a_symlinked_backup_folder_fails(host):
    backups_host(host, skip=(BACKUPS,))
    elsewhere = host.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (host.root / "data" / BACKUPS).symlink_to(elsewhere)
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "symbolic link" in result.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to give the folder to another user")
def test_a_backup_folder_of_the_wrong_owner_fails(host):
    backups_host(host)
    os.chown(host.root / "data" / BACKUPS, 0, 0)
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "expected" in result.stdout


def custom_dir(host, relative: str, mode: int = 0o700):
    folder = host.root / relative.lstrip("/")
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(mode)
    host.write_env(YTP_BACKUP_DIR="/" + relative.lstrip("/"))
    return folder


def test_a_custom_backup_folder_from_the_env_file_is_checked_instead(host):
    backups_host(host, skip=(BACKUPS,))
    custom_dir(host, "mnt/nas/ytp-backups")
    result = host.run("--only", "backups")
    assert status(result, "backups") == "PASS"
    assert "YTP_BACKUP_DIR" in result.stdout


@pytest.mark.security
@pytest.mark.parametrize(
    "inside", ["data/Filme/backups", "data/downloads/movies/b", "data/Podcasts"]
)
def test_a_backup_folder_inside_a_library_or_the_downloads_fails(host, inside):
    backups_host(host, skip=(BACKUPS,))
    custom_dir(host, inside)
    result = host.run("--only", "backups")
    assert status(result, "backups") == "FAIL"
    assert "inside" in result.stdout


def test_a_custom_folder_with_loose_mode_fails_too(host):
    backups_host(host, skip=(BACKUPS,))
    custom_dir(host, "mnt/nas/ytp-backups", mode=0o755)
    assert status(host.run("--only", "backups"), "backups") == "FAIL"


def test_the_full_run_includes_the_backup_check(host):
    backups_host(host)
    result = host.run()
    assert status(result, "backups") == "PASS"
    assert "backups" in result.stdout
