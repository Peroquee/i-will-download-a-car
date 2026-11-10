"""The whole installer conversation, played with a scripted terminal and a recording runner (#37)."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from ytp_install import cli
from ytp_install.envfile import parse_env
from ytp_install.runner import Result
from ytp_install.steps import STEP_IDS, State

from tests.helpers.installer import (
    SECRETS,
    FakeRunner,
    FakeTerminal,
    deps,
    make_repo,
    new_install_rules,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def ovpn(tmp_path) -> Path:
    path = tmp_path / "my_vpn.ovpn"
    path.write_text("client\ndev tun\nremote vpn.example.net 1195\n<key>SECRETKEYDATA</key>\n")
    return path


def play(repo, tmp_path, rules, argv=(), runner=None, **overrides):
    term = FakeTerminal(rules)
    runner = runner or FakeRunner(repo, dry_run="--dry-run" in argv)
    code = cli.main(["--lang", "en", *argv], repo, deps(term, runner, tmp_path, **overrides))
    return code, term, runner


def whole_output(term, runner) -> str:
    return term.text + "\n" + "\n".join(runner.lines)


# --- a new installation ----------------------------------------------------------------------------


def test_a_new_installation_runs_every_step_in_order(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, new_install_rules(ovpn))
    assert code == 0, term.text
    order = [
        runner.index("apt-get update"),
        runner.index("bootstrap.sh"),
        runner.index("create-layout.sh"),
        runner.index("check-host.sh --only layout"),
        runner.index("render_ovpn.py"),
        runner.index("set_password.py"),
        runner.index("seed_config.py"),
        runner.index("setup_key.py"),
        runner.index("docker compose up -d --build"),
        runner.index("setup_categories.py"),
        runner.index("verify_proxy.py"),
    ]
    assert order == sorted(order), "the steps ran in the wrong order"
    assert runner.index("check-host.sh --env-file") > runner.index("setup_categories.py")
    assert State.load(repo / "config" / "installer-state.json").completed.keys() == set(STEP_IDS)


def test_the_commands_get_the_users_answers(repo, tmp_path, ovpn):
    _, _, runner = play(repo, tmp_path, new_install_rules(ovpn))
    bootstrap = runner.ran("bootstrap.sh")[0]
    assert bootstrap[bootstrap.index("--user") + 1] == "pat"
    assert bootstrap[bootstrap.index("--data-root") + 1] == "/data"
    assert runner.ran("apt-get install")[0][-4:] == ["ca-certificates", "curl", "git", "make"]
    proxy = runner.ran("verify_proxy.py")[0]
    assert proxy[proxy.index("--host") + 1] == "192.168.178.20"


def test_the_env_file_is_written_with_the_answers_and_mode_600(repo, tmp_path, ovpn):
    play(repo, tmp_path, new_install_rules(ovpn))
    env_path = repo / ".env"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    env = parse_env(env_path.read_text())
    assert env["VPN_SERVICE_PROVIDER"] == "custom"
    assert env["OPENVPN_USER"] == "vpn-user"
    assert env["OPENVPN_PASSWORD"] == SECRETS["vpn_password"]
    assert env["PROXY_BIND"] == "192.168.178.20"
    assert env["LAN_SUBNET"] == "192.168.178.0/24"
    assert env["PROXY_USER"] == "admin"
    assert env["QBIT_PASSWORD"] == SECRETS["qbit"], "generated, not typed"
    assert env["JELLYFIN_URL"] == "http://192.168.178.108:8096"
    assert env["JELLYFIN_API_KEY"] == SECRETS["jellyfin_key"]
    assert env["ALERT_URL"] == SECRETS["alert_url"]
    assert env["MEDIA_DIR_PODCASTS"] == "Podcasts"
    assert env["TZ"] == "Europe/Berlin"
    assert "PROXY_PASSWORD_HASH" in env, "the rest of the template is kept"


def test_the_ovpn_file_is_copied_private_and_rendered(repo, tmp_path, ovpn):
    _, _, runner = play(repo, tmp_path, new_install_rules(ovpn))
    copy = repo / "config" / "expressvpn.ovpn"
    assert copy.read_text() == ovpn.read_text()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert runner.ran("render_ovpn.py")


def test_passwords_go_in_through_stdin_only(repo, tmp_path, ovpn):
    _, _, runner = play(repo, tmp_path, new_install_rules(ovpn))
    by_script = {Path(argv[0]).name: text for argv, text in runner.inputs}
    assert by_script["set_password.py"] == SECRETS["web_password"] + "\n"
    assert by_script["seed_config.py"] == SECRETS["qbit"] + "\n"
    assert by_script["setup_categories.py"] == SECRETS["qbit"] + "\n"
    assert by_script["verify_proxy.py"] == SECRETS["web_password"] + "\n"
    for command in runner.commands:
        for secret in SECRETS.values():
            assert secret not in " ".join(command), command


def test_no_secret_is_shown_or_logged(repo, tmp_path, ovpn):
    _, term, runner = play(repo, tmp_path, new_install_rules(ovpn))
    log = whole_output(term, runner)
    for name, secret in SECRETS.items():
        assert secret not in log, f"{name} leaked into what is shown or logged"
    assert "SECRETKEYDATA" not in log


def test_the_summary_hides_secrets_and_the_end_explains_what_to_do(repo, tmp_path, ovpn):
    _, term, _ = play(repo, tmp_path, new_install_rules(ovpn))
    text = term.text
    assert "OPENVPN_PASSWORD = (set)" in text
    assert "Open in your browser:  http://192.168.178.20/" in text
    assert 'user "admin"' in text
    assert "AI-HELP.md" in text
    assert "sudo make backup" in text
    assert "/data/.youtube-premium-backups, on the media drive" in text


def test_the_german_conversation(repo, tmp_path, ovpn):
    rules = [(n, a) for n, a in new_install_rules(ovpn)]
    rules[1:] = [
        ("Wie bekommst du die Zugangsdaten", "1"),
        ("Pfad der .ovpn-Datei", str(ovpn)),
        ("VPN-Benutzername", "vpn-user"),
        ("VPN-Passwort", SECRETS["vpn_password"]),
        ("Passwort des Administrators", SECRETS["web_password"]),
        ("Passwort wiederholen", SECRETS["web_password"]),
        ("Adresse deines Jellyfin", ""),
    ]
    term = FakeTerminal(rules)
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "de"], repo, deps(term, runner, tmp_path))
    assert code == 0, term.text
    assert "Öffne im Browser:" in term.text
    assert "Schritt 1 von 8" in term.text


def test_a_builtin_wireguard_provider(repo, tmp_path):
    rules = [
        ("How do you get your details", "2"),
        ("Provider name in lower case", "mullvad"),
        ("Connection type", "2"),
        ("private key", "wgwg-wgwg-wgwg"),
        ("your address in the VPN", "10.64.222.21/32"),
        ("Country of the VPN server", "Germany"),
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(repo, tmp_path, rules)
    assert code == 0, term.text
    env = parse_env((repo / ".env").read_text())
    assert (env["VPN_SERVICE_PROVIDER"], env["VPN_TYPE"], env["VPN_SERVER_COUNTRIES"]) == (
        "mullvad",
        "wireguard",
        "Germany",
    )
    assert env["WIREGUARD_PRIVATE_KEY"] == "wgwg-wgwg-wgwg"
    assert env["OPENVPN_CUSTOM_CONFIG"] == ""
    assert not runner.ran("render_ovpn.py"), "a built-in provider needs no file"
    assert "wgwg-wgwg-wgwg" not in whole_output(term, runner)


def test_a_wrong_answer_is_explained_and_asked_again(repo, tmp_path, ovpn):
    rules = new_install_rules(ovpn)
    rules = [(n, a) for n, a in rules if n != "Password of the administrator"]
    rules.append(
        (
            "Password of the administrator",
            ["short", "has'quote-longenough", SECRETS["web_password"]],
        )
    )
    code, term, _ = play(repo, tmp_path, rules)
    assert code == 0
    assert "That is too short." in term.text
    assert "cannot be used here because it breaks the settings file" in term.text


def test_two_different_passwords_are_refused(repo, tmp_path, ovpn):
    rules = [r for r in new_install_rules(ovpn) if r[0] != "Repeat the password"]
    rules.append(("Repeat the password", ["not-the-same-pass-1", SECRETS["web_password"]]))
    code, term, _ = play(repo, tmp_path, rules)
    assert code == 0
    assert "The two entries are different." in term.text


def test_a_file_that_is_not_an_ovpn_is_refused(repo, tmp_path, ovpn):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello\n")
    rules = [r for r in new_install_rules(ovpn) if r[0] != "Path of the .ovpn file"]
    rules.append(("Path of the .ovpn file", [str(bad), str(tmp_path / "missing.ovpn"), str(ovpn)]))
    code, term, _ = play(repo, tmp_path, rules)
    assert code == 0
    assert "does not look like an .ovpn file" in term.text
    assert "does not exist or cannot be read" in term.text


def test_the_user_can_say_no_at_the_summary(repo, tmp_path, ovpn):
    rules = [*new_install_rules(ovpn), ("Install now?", "n")]
    code, _, runner = play(repo, tmp_path, rules)
    assert code == 0
    assert runner.commands == []
    assert not (repo / ".env").exists()


def test_the_lan_is_asked_when_it_cannot_be_detected(repo, tmp_path, ovpn):
    rules = [
        *new_install_rules(ovpn),
        ("address in your home network", "192.168.1.50"),
        ("Your home network", "192.168.1.0/24"),
    ]
    code, term, _ = play(repo, tmp_path, rules, detect=lambda runner: None)
    assert code == 0, term.text
    env = parse_env((repo / ".env").read_text())
    assert (env["PROXY_BIND"], env["LAN_SUBNET"]) == ("192.168.1.50", "192.168.1.0/24")


def test_an_address_outside_the_network_is_refused(repo, tmp_path, ovpn):
    rules = [
        *new_install_rules(ovpn),
        ("address in your home network", "192.168.1.50"),
        ("Your home network", ["10.0.0.0/24", "192.168.1.0/24"]),
    ]
    _, term, _ = play(repo, tmp_path, rules, detect=lambda runner: None)
    assert "The address is not in that network." in term.text


# --- the system -------------------------------------------------------------------------------------


def test_another_system_is_refused_before_anything_happens(repo, tmp_path):
    term = FakeTerminal()
    runner = FakeRunner(repo)
    d = deps(term, runner, tmp_path)
    d.os_release.write_text('ID=ubuntu\nVERSION_CODENAME=jammy\nPRETTY_NAME="Ubuntu 22.04"\n')
    assert cli.main(["--lang", "en"], repo, d) == 1
    assert "Debian 12" in term.text
    assert "Ubuntu 22.04" in term.text
    assert runner.commands == []


def test_without_root_the_installer_asks_for_sudo(repo, tmp_path):
    term = FakeTerminal()
    runner = FakeRunner(repo)
    d = deps(term, runner, tmp_path, require_root=True)
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert cli.main(["--lang", "en"], repo, d) == 1
    assert "sudo ./install.sh" in term.text


def test_the_language_is_asked_first_and_remembered(repo, tmp_path, ovpn):
    term = FakeTerminal(new_install_rules(ovpn))
    runner = FakeRunner(repo)
    assert cli.main([], repo, deps(term, runner, tmp_path)) == 0
    assert term.transcript[0].startswith("? Sprache / Language")
    assert State.load(repo / "config" / "installer-state.json").language == "en"


# --- dry run ----------------------------------------------------------------------------------------


def test_a_dry_run_changes_nothing(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, new_install_rules(ovpn), argv=["--dry-run"])
    assert code == 0, term.text
    assert runner.commands == [], "nothing was executed"
    assert not (repo / ".env").exists()
    assert not (repo / "config").exists()
    shown = "\n".join(runner.lines)
    assert "+ " in shown
    assert "bootstrap.sh" in shown
    assert "docker compose up -d --build" in shown
    assert SECRETS["web_password"] not in whole_output(term, runner)
    assert "TEST RUN" in term.text


# --- failure and resume ------------------------------------------------------------------------------


def test_a_failing_step_stops_with_a_plain_hint_and_keeps_the_finished_ones(repo, tmp_path, ovpn):
    runner = FakeRunner(repo)
    runner.fail(
        "docker compose up", 1, "dependency failed to start: container gluetun is unhealthy"
    )
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 1
    assert (
        'The step "Build and start the services, wait until everything runs" failed.' in term.text
    )
    assert "VPN tunnel is not connected" in term.text, "not 'wrong credentials' for every failure"
    assert "wrong VPN credentials" in term.text, "still named as one possible cause"
    assert 'grep -v "http server"' in term.text, "the control server's request lines hide the error"
    assert "never the file .env" in term.text
    assert "config/installer.log" in term.text
    state = State.load(repo / "config" / "installer-state.json")
    assert set(state.completed) == {"prepare", "config", "folders", "vpn", "logins"}
    assert SECRETS["vpn_password"] not in whole_output(term, runner)


def test_the_next_run_continues_with_the_failed_step_and_asks_nothing_again(repo, tmp_path, ovpn):
    broken = FakeRunner(repo)
    broken.fail("docker compose up", 1)
    play(repo, tmp_path, new_install_rules(ovpn), runner=broken)
    code, term, runner = play(repo, tmp_path, [])
    assert code == 0, term.text
    assert "stopped at the step" in term.text
    assert "Build and start the services" in term.text
    assert "(already done)" in term.text
    assert not runner.ran("bootstrap.sh")
    assert not runner.ran("set_password.py")
    assert runner.ran("docker compose up -d --build")
    assert runner.ran("setup_categories.py")
    assert all("To test the login" in p or "VPN lock" in p for p in term.unanswered), (
        term.unanswered
    )


def test_a_resume_before_the_logins_asks_for_the_web_password_again(repo, tmp_path, ovpn):
    broken = FakeRunner(repo)
    broken.fail("seed_config.py", 1)
    play(repo, tmp_path, new_install_rules(ovpn), runner=broken)
    rules = [
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(repo, tmp_path, rules)
    assert code == 0, term.text
    assert runner.inputs[0][1] == SECRETS["web_password"] + "\n"
    assert not runner.ran("create-layout.sh")


def test_services_that_never_get_healthy_end_in_a_clear_message(repo, tmp_path, ovpn):
    import json

    unhealthy = "\n".join(
        json.dumps(
            {
                "Service": s,
                "State": "running",
                "Health": "unhealthy" if s == "gluetun" else "healthy",
            }
        )
        for s in ("gluetun", "qbittorrent", "app", "proxy", "watchdog")
    )
    runner = FakeRunner(repo)
    runner.compose_ps = [unhealthy]
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 1
    assert "After 30 seconds not all services run yet: gluetun." in term.text


def test_services_that_become_healthy_after_a_while_are_waited_for(repo, tmp_path, ovpn):
    import json

    from tests.helpers.installer import healthy_ps

    starting = "\n".join(
        json.dumps({"Service": s, "State": "running", "Health": "starting"})
        for s in ("gluetun", "qbittorrent", "app", "proxy", "watchdog")
    )
    runner = FakeRunner(repo)
    runner.compose_ps = [starting, starting, healthy_ps()]
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 0, term.text
    assert "All services are running." in term.text


def test_ctrl_c_ends_politely(repo, tmp_path):
    class Boom(FakeTerminal):
        def ask(self, prompt, default=""):
            raise KeyboardInterrupt

    runner = FakeRunner(repo)
    term = Boom()
    assert cli.main(["--lang", "en"], repo, deps(term, runner, tmp_path)) == 130
    assert "Stopped." in term.text


# --- an existing installation -------------------------------------------------------------------------


@pytest.fixture
def installed(repo, tmp_path, ovpn):
    play(repo, tmp_path, new_install_rules(ovpn))
    (repo / ".env").write_text((repo / ".env").read_text() + "\n# my own note\nMY_EXTRA=keep-me\n")
    return repo


def test_an_existing_installation_offers_a_menu(installed, tmp_path):
    code, term, runner = play(installed, tmp_path, [("1 = Change settings", "5")])
    assert code == 0
    assert "There is already an installation here." in term.text
    assert "Change settings" in term.text
    assert runner.commands == []


def test_an_installation_older_than_the_installer_gets_the_menu_too(repo, tmp_path):
    (repo / ".env").write_text((repo / ".env.example").read_text())
    code, term, _ = play(repo, tmp_path, [("1 = Change settings", "5")])
    assert code == 0
    assert "There is already an installation here." in term.text


def test_changing_settings_keeps_secrets_and_manual_edits_and_backs_up(installed, tmp_path):
    rules = [
        ("1 = Change settings", "1"),
        ("How do you get your details", ""),
        ("Path of the .ovpn file", ""),
        ("User name of the administrator", "pat"),
        ("Change the administrator", "n"),
    ]
    before = (installed / ".env").read_text()
    code, term, runner = play(installed, tmp_path, rules)
    assert code == 0, term.text
    env = parse_env((installed / ".env").read_text())
    assert env["PROXY_USER"] == "pat"
    assert env["OPENVPN_PASSWORD"] == SECRETS["vpn_password"], "Enter kept the stored secret"
    assert env["MY_EXTRA"] == "keep-me"
    assert "# my own note" in (installed / ".env").read_text()
    backups = sorted(installed.glob(".env.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == before
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "was saved as .env.bak-" in term.text
    assert not runner.ran("bootstrap.sh"), "Docker is not installed again"
    assert runner.ran("docker compose up -d --build")


def test_nothing_is_backed_up_when_nothing_changed(installed, tmp_path):
    rules = [
        ("1 = Change settings", "1"),
        ("Path of the .ovpn file", ""),
        ("Change the administrator", "n"),
    ]
    play(installed, tmp_path, rules)
    assert list(installed.glob(".env.bak-*")) == []


def test_repair_runs_every_step_again_without_asking_for_settings(installed, tmp_path):
    code, term, runner = play(installed, tmp_path, [("1 = Change settings", "3")])
    assert code == 0, term.text
    for needle in (
        "bootstrap.sh",
        "create-layout.sh",
        "docker compose up -d --build",
        "setup_categories.py",
    ):
        assert runner.ran(needle), needle
    assert not runner.ran("seed_config.py"), "qBittorrent already has its login"
    assert not runner.ran("set_password.py"), "the web login is already set"


def test_the_repair_flag(installed, tmp_path):
    code, _, runner = play(installed, tmp_path, [], argv=["--repair"])
    assert code == 0
    assert runner.ran("create-layout.sh")


def test_the_state_view_lists_steps_and_services(installed, tmp_path):
    code, term, runner = play(installed, tmp_path, [], argv=["--status"])
    assert code == 0
    assert "✓ Install Docker and the basic programs" in term.text
    assert "gluetun: running healthy" in term.text
    assert not runner.ran("up")


# --- no questions: the answers file --------------------------------------------------------------------


def answers_file(tmp_path, ovpn, mode=0o600, extra=""):
    path = tmp_path / "answers"
    path.write_text(
        f"vpn_kind=ovpn\novpn_source={ovpn}\nvpn_user=vpn-user\nvpn_password={SECRETS['vpn_password']}\n"
        f"web_password={SECRETS['web_password']}\n{extra}"
    )
    path.chmod(mode)
    return path


def test_an_answers_file_installs_without_a_single_question(repo, tmp_path, ovpn):
    path = answers_file(tmp_path, ovpn, extra="MEDIA_DIR_MUSIC=Songs\n")
    code, term, runner = play(repo, tmp_path, [], argv=["--answers", str(path)])
    assert code == 0, term.text
    assert term.unanswered == []
    assert parse_env((repo / ".env").read_text())["MEDIA_DIR_MUSIC"] == "Songs"
    assert not [p for p in term.transcript if p.startswith("?")]
    assert runner.ran("setup_categories.py")


def test_an_answers_file_that_others_can_read_is_refused(repo, tmp_path, ovpn):
    path = answers_file(tmp_path, ovpn, mode=0o644)
    code, term, runner = play(repo, tmp_path, [], argv=["--answers", str(path)])
    assert code == 2
    assert "chmod 600" in term.text
    assert runner.commands == []


def test_an_answers_file_with_problems_lists_them_all(repo, tmp_path, ovpn):
    path = answers_file(tmp_path, ovpn, extra="web_user=x\nbogus=1\nMEDIA_DIR_MUSIC=Filme\n")
    code, term, _ = play(repo, tmp_path, [], argv=["--answers", str(path)])
    assert code == 2
    assert "unknown answer: bogus" in term.text
    assert "web_user" in term.text
    assert "dirs" in term.text


def test_the_answers_file_is_never_echoed(repo, tmp_path, ovpn):
    path = answers_file(tmp_path, ovpn)
    _, term, runner = play(repo, tmp_path, [], argv=["--answers", str(path)])
    assert SECRETS["web_password"] not in whole_output(term, runner)


# --- update ---------------------------------------------------------------------------------------------


def test_update_backs_up_first_then_pulls_then_rebuilds(installed, tmp_path):
    (installed / ".git").mkdir()
    code, term, runner = play(installed, tmp_path, [], argv=["--update"])
    assert code == 0, term.text
    order = [
        runner.index("backup.py create"),
        runner.index("git pull --ff-only"),
        runner.index("render_ovpn.py"),
        runner.index("docker compose up -d --build"),
        runner.index("check-host.sh"),
    ]
    assert order == sorted(order)
    assert "Update finished." in term.text


def test_a_failed_backup_stops_the_update_before_anything_changes(installed, tmp_path):
    (installed / ".git").mkdir()
    runner = FakeRunner(installed)
    runner.fail("backup.py", 1)
    code, term, _ = play(installed, tmp_path, [], argv=["--update"], runner=runner)
    assert code == 1
    assert "The backup failed, so nothing is updated." in term.text
    assert not runner.ran("git pull")
    assert not runner.ran("docker compose up")


def test_a_failed_pull_stops_the_update(installed, tmp_path):
    (installed / ".git").mkdir()
    runner = FakeRunner(installed)
    runner.fail("git pull", 1)
    code, term, _ = play(installed, tmp_path, [], argv=["--update"], runner=runner)
    assert code == 1
    assert "git pull" in term.text
    assert not runner.ran("docker compose up")


def test_update_adds_new_template_settings_without_touching_the_owners(installed, tmp_path):
    template = (
        installed / ".env.example"
    ).read_text() + "\nBRAND_NEW_SETTING=on\nANOTHER_SECRET=changeme\n"
    (installed / ".env.example").write_text(template)
    before = parse_env((installed / ".env").read_text())
    code, term, _ = play(installed, tmp_path, [], argv=["--update"])
    assert code == 0, term.text
    after = parse_env((installed / ".env").read_text())
    assert after["BRAND_NEW_SETTING"] == "on"
    assert after["ANOTHER_SECRET"] == "changeme"
    assert {k: v for k, v in after.items() if k in before} == before
    assert "BRAND_NEW_SETTING" in term.text
    assert "a value is still missing for: ANOTHER_SECRET" in term.text
    assert len(list(installed.glob(".env.bak-*"))) == 1


def test_update_without_an_installation_says_so(repo, tmp_path):
    code, term, runner = play(repo, tmp_path, [], argv=["--update"])
    assert code == 1
    assert "Nothing is installed here yet" in term.text
    assert runner.commands == []


def test_update_does_not_pull_as_root_but_as_the_folder_owner(installed, tmp_path, monkeypatch):
    (installed / ".git").mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _, _, runner = play(installed, tmp_path, [], argv=["--update"])
    assert runner.ran("runuser -u pat -- git pull --ff-only")


def test_the_log_keeps_commands_but_never_their_input(repo, tmp_path, ovpn):
    from ytp_install.runner import file_logger

    log = repo / "config" / "installer.log"
    runner = FakeRunner(repo)
    runner._log = file_logger(log, lambda: "T")
    play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    text = log.read_text()
    assert "set_password.py" in text
    for secret in SECRETS.values():
        assert secret not in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_json_state_has_no_secrets(repo, tmp_path, ovpn):
    play(repo, tmp_path, new_install_rules(ovpn))
    raw = (repo / "config" / "installer-state.json").read_text()
    data = json.loads(raw)
    assert set(data) == {"version", "language", "completed"}
    for secret in SECRETS.values():
        assert secret not in raw


def test_a_failing_helper_script_message_names_the_step_and_not_the_password(repo, tmp_path, ovpn):
    runner = FakeRunner(repo)
    runner.rules.append(
        ("set_password.py", Result(1, "", f"error mentioning {SECRETS['web_password']}"))
    )
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 1
    assert 'The step "Set up the logins (web page, qBittorrent, watchdog)" failed.' in term.text


def test_a_dry_run_ends_with_a_note_not_with_done(repo, tmp_path, ovpn):
    _, term, _ = play(repo, tmp_path, new_install_rules(ovpn), argv=["--dry-run"])
    assert "Test run finished: nothing was changed." in term.text
    assert "Open in your browser" not in term.text


def test_no_warning_about_the_vpn_type_that_is_not_used(repo, tmp_path, ovpn):
    _, term, _ = play(repo, tmp_path, new_install_rules(ovpn))
    assert "still missing" not in term.text
    assert "WIREGUARD" not in term.text


# --- qBittorrent behind the proxy: the Web UI settings go in through its API ---------------------


def test_the_settings_for_the_proxy_are_applied_through_the_api_without_stopping_qbittorrent(
    repo, tmp_path, ovpn
):
    _, _, runner = play(repo, tmp_path, new_install_rules(ovpn))
    assert runner.ran("setup_categories.py")
    assert not runner.ran("compose stop qbittorrent")
    assert not runner.ran("--fix-host-header")


def test_repair_applies_them_again_after_the_stack_is_up(repo, tmp_path, ovpn):
    play(repo, tmp_path, new_install_rules(ovpn))
    _, _, runner = play(repo, tmp_path, [("1 = Change settings", "3")])
    assert runner.index("docker compose up -d --build") < runner.index("setup_categories.py")
    assert not runner.ran("compose stop qbittorrent")


def test_update_applies_them_after_the_new_version_is_up_and_before_the_host_checks(
    repo, tmp_path, ovpn
):
    play(repo, tmp_path, new_install_rules(ovpn))
    _, _, runner = play(repo, tmp_path, [], argv=["--update"])
    up = max(i for i, c in enumerate(runner.commands) if c[:4] == ["docker", "compose", "up", "-d"])
    api = max(i for i, c in enumerate(runner.commands) if "setup_categories.py" in " ".join(c))
    checks = runner.index("check-host.sh --env-file")
    assert up < api < checks
    stdin = [text for argv, text in runner.inputs if "setup_categories.py" in " ".join(argv)]
    assert stdin
    assert stdin[-1].strip() == SECRETS["qbit"], "the password goes in through stdin"


def test_update_without_a_qbittorrent_password_skips_the_step_instead_of_failing(
    repo, tmp_path, ovpn
):
    play(repo, tmp_path, new_install_rules(ovpn))
    env = (repo / ".env").read_text().replace(f"QBIT_PASSWORD={SECRETS['qbit']}", "QBIT_PASSWORD=")
    (repo / ".env").write_text(env)
    code, term, runner = play(repo, tmp_path, [], argv=["--update"])
    assert code == 0, term.text
    assert len(runner.ran("setup_categories.py")) == 0


def test_a_failing_api_step_stops_the_update_with_its_hint(repo, tmp_path, ovpn):
    play(repo, tmp_path, new_install_rules(ovpn))
    runner = FakeRunner(repo)
    runner.fail("setup_categories.py", 1, "boom")
    code, _, _ = play(repo, tmp_path, [], argv=["--update"], runner=runner)
    assert code == 1


# --- the last step failed: offer to repair (steps 5 to 8) instead of just stopping ---------------


class FlakyRunner(FakeRunner):
    """Like FakeRunner, but a command can fail the first N times and succeed afterwards."""

    def __init__(self, root, failures: dict[str, int]):
        super().__init__(root)
        self.failures = dict(failures)

    def _execute(self, argv, input_text, stream, timeout, cwd):
        joined = " ".join(argv)
        for needle, left in self.failures.items():
            if needle in joined and left > 0:
                self.failures[needle] = left - 1
                self.commands.append(list(argv))
                return Result(1, "", "boom")
        return super()._execute(argv, input_text, stream, timeout, cwd)


CHECKS = "check-host.sh --env-file"
MENU = "Repair (run steps 5 to 8 again)"


def test_a_failed_check_offers_the_menu_and_repair_runs_steps_5_to_8_again(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {CHECKS: 1})
    rules = [*new_install_rules(ovpn), (MENU, "1")]
    code, term, _ = play(repo, tmp_path, rules, runner=runner)
    assert code == 0, term.text
    assert "1 = Repair" in term.text
    assert len(runner.ran("docker compose up -d --build")) == 2, "start (6) ran again"
    assert len(runner.ran("setup_categories.py")) == 2, "qBittorrent (7) ran again"
    assert len(runner.ran(CHECKS)) == 2, "the checks (8) ran again and passed"
    assert len(runner.ran("bootstrap.sh")) == 1, "steps 1 to 4 did not"
    assert len(runner.ran("render_ovpn.py")) == 1
    assert len(runner.ran("set_password.py")) == 2, (
        "the same web password is stored again (idempotent)"
    )
    assert "invisible" not in term.text.split("1 = Repair")[1], "and nothing is asked again"
    assert "Done!" in term.text
    state = State.load(repo / "config" / "installer-state.json")
    assert set(state.completed) == set(STEP_IDS)


def test_only_check_again_runs_just_the_last_step(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {CHECKS: 1})
    rules = [*new_install_rules(ovpn), (MENU, "2")]
    code, term, _ = play(repo, tmp_path, rules, runner=runner)
    assert code == 0, term.text
    assert len(runner.ran(CHECKS)) == 2
    assert len(runner.ran("docker compose up -d --build")) == 1
    assert len(runner.ran("setup_categories.py")) == 1


def test_quit_from_the_menu_ends_with_the_failure_and_a_resumable_state(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {CHECKS: 5})
    rules = [*new_install_rules(ovpn), (MENU, "3")]
    code, term, _ = play(repo, tmp_path, rules, runner=runner)
    assert code == 1
    assert len(runner.ran(CHECKS)) == 1
    assert "failed" in term.text
    state = State.load(repo / "config" / "installer-state.json")
    assert "checks" not in state.completed
    assert "qbit" in state.completed


def test_pressing_enter_in_the_menu_means_repair(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {CHECKS: 1})
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 0, term.text
    assert len(runner.ran("setup_categories.py")) == 2


def test_a_repair_that_fails_again_offers_the_menu_again(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {CHECKS: 2})
    rules = [*new_install_rules(ovpn), (MENU, ["1", "3"])]
    code, term, _ = play(repo, tmp_path, rules, runner=runner)
    assert code == 1
    assert term.text.count("1 = Repair") == 2
    assert len(runner.ran(CHECKS)) == 2


def test_no_menu_for_a_step_other_than_the_checks(repo, tmp_path, ovpn):
    runner = FlakyRunner(repo, {"docker compose up": 1})
    code, term, _ = play(repo, tmp_path, new_install_rules(ovpn), runner=runner)
    assert code == 1
    assert "1 = Repair" not in term.text


def test_no_menu_without_a_person_an_answers_file_run_just_fails(repo, tmp_path, ovpn):
    answers = tmp_path / "answers"
    answers.write_text(
        "web_password=web-web-web-2x\nvpn_user=vpn-user\nvpn_password=vpn-vpn-vpn-1\n"
        f"ovpn_source={ovpn}\n"
    )
    answers.chmod(0o600)
    runner = FlakyRunner(repo, {CHECKS: 1})
    term = FakeTerminal()
    code = cli.main(["--lang", "en", "--answers", str(answers)], repo, deps(term, runner, tmp_path))
    assert code == 1
    assert "1 = Repair" not in term.text
    assert len(runner.ran(CHECKS)) == 1


def test_a_rerun_after_a_failed_check_offers_the_menu_before_resuming(repo, tmp_path, ovpn):
    play(
        repo,
        tmp_path,
        [*new_install_rules(ovpn), (MENU, "3")],
        runner=FlakyRunner(repo, {CHECKS: 9}),
    )
    runner = FakeRunner(repo)
    code, term, _ = play(repo, tmp_path, [(MENU, "1")], runner=runner)
    assert code == 0, term.text
    assert "stopped at the step" in term.text
    assert "1 = Repair" in term.text
    assert len(runner.ran("setup_categories.py")) == 1, "the repair of step 7 ran this time"
    assert len(runner.ran("docker compose up -d --build")) == 1
    assert not runner.ran("bootstrap.sh")
    assert not runner.ran("set_password.py")


def test_a_rerun_can_just_check_or_quit(repo, tmp_path, ovpn):
    play(
        repo,
        tmp_path,
        [*new_install_rules(ovpn), (MENU, "3")],
        runner=FlakyRunner(repo, {CHECKS: 9}),
    )
    runner = FakeRunner(repo)
    code, _, _ = play(repo, tmp_path, [(MENU, "2")], runner=runner)
    assert code == 0
    assert len(runner.ran(CHECKS)) == 1
    assert not runner.ran("setup_categories.py")
    # and quitting changes nothing
    play(repo, tmp_path, [*new_install_rules(ovpn)], runner=FlakyRunner(repo, {CHECKS: 9}))
    state_before = State.load(repo / "config" / "installer-state.json").completed
    runner = FakeRunner(repo)
    code, _, _ = play(repo, tmp_path, [(MENU, "3")], runner=runner)
    assert code == 0
    assert runner.commands == []
    assert State.load(repo / "config" / "installer-state.json").completed == state_before


def test_a_rerun_interrupted_at_another_step_does_not_show_the_menu(repo, tmp_path, ovpn):
    play(
        repo, tmp_path, new_install_rules(ovpn), runner=FlakyRunner(repo, {"docker compose up": 1})
    )
    code, term, _ = play(repo, tmp_path, [])
    assert code == 0, term.text
    assert "1 = Repair" not in term.text


# --- the app's accounts (#44) --------------------------------------------------------------------------


def before_accounts(repo) -> None:
    """The .env of an installation from before the accounts: no admin settings at all."""
    keep = [
        ln for ln in (repo / ".env").read_text().splitlines() if not ln.startswith("APP_ADMIN_")
    ]
    (repo / ".env").write_text("\n".join(keep) + "\n")


def admin_runs(runner) -> list[list[str]]:
    return runner.ran("set_admin.py")


def test_a_new_installation_makes_the_first_admin_from_the_web_login(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, new_install_rules(ovpn))
    assert code == 0, term.text
    assert runner.index("set_password.py") < runner.index("set_admin.py")
    (command,) = admin_runs(runner)
    assert command[command.index("--user") + 1] == "admin"
    assert "--password-stdin" in command
    assert SECRETS["web_password"] not in " ".join(command), "never on a command line"
    assert [i for c, i in runner.inputs if "set_admin.py" in " ".join(c)] == [
        SECRETS["web_password"] + "\n"
    ]
    env = parse_env((repo / ".env").read_text())
    assert env["APP_ADMIN_PASSWORD_HASH"].startswith("scrypt:")
    assert env["APP_ADMIN_USER"] == env["PROXY_USER"]
    assert SECRETS["web_password"] not in whole_output(term, runner)


def test_the_admin_texts_name_the_account_not_just_a_page_password(repo, tmp_path, ovpn):
    _, term, _ = play(repo, tmp_path, new_install_rules(ovpn))
    assert "administrator account" in term.text
    assert 'You create more accounts there under "Accounts".' in term.text


def test_changing_the_password_sets_the_admin_hash_again(installed, tmp_path):
    rules = [
        ("1 = Change settings", "1"),
        ("Path of the .ovpn file", ""),
        ("Change the administrator", "y"),
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(installed, tmp_path, rules)
    assert code == 0, term.text
    assert len(admin_runs(runner)) == 1
    assert len(runner.ran("set_password.py")) == 1


def test_keeping_the_password_does_not_touch_the_admin(installed, tmp_path):
    rules = [
        ("1 = Change settings", "1"),
        ("Path of the .ovpn file", ""),
        ("Change the administrator", "n"),
    ]
    _, _, runner = play(installed, tmp_path, rules)
    assert admin_runs(runner) == []


def test_update_of_an_older_installation_asks_the_password_once_and_keeps_the_name(
    installed, tmp_path
):
    before_accounts(installed)
    env = parse_env((installed / ".env").read_text())
    rules = [
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(installed, tmp_path, rules, argv=["--update"])
    assert code == 0, term.text
    (command,) = admin_runs(runner)
    assert command[command.index("--user") + 1] == env["PROXY_USER"]
    assert runner.index("set_admin.py") < runner.index("docker compose up -d --build")
    assert "becomes the first" in term.text
    assert "a value is still missing for: APP_ADMIN_PASSWORD_HASH" not in term.text
    after = parse_env((installed / ".env").read_text())
    assert after["APP_ADMIN_PASSWORD_HASH"].startswith("scrypt:")
    assert SECRETS["web_password"] not in whole_output(term, runner)


def test_a_second_update_asks_nothing_and_makes_no_new_admin(installed, tmp_path):
    before_accounts(installed)
    rules = [
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    play(installed, tmp_path, rules, argv=["--update"])
    code, term, runner = play(installed, tmp_path, [], argv=["--update"])
    assert code == 0, term.text
    assert admin_runs(runner) == []
    assert "becomes the first" not in term.text


def test_an_update_of_an_installation_that_has_an_admin_asks_nothing(installed, tmp_path):
    code, term, runner = play(installed, tmp_path, [], argv=["--update"])
    assert code == 0, term.text
    assert admin_runs(runner) == []


def test_update_falls_back_to_admin_when_the_old_login_name_is_no_account_name(installed, tmp_path):
    before_accounts(installed)
    text = (installed / ".env").read_text()
    (installed / ".env").write_text(text.replace("PROXY_USER=admin", "PROXY_USER=-x"))
    rules = [
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(installed, tmp_path, rules, argv=["--update"])
    assert code == 0, term.text
    (command,) = admin_runs(runner)
    assert command[command.index("--user") + 1] == "admin"


def test_a_weak_password_is_asked_again_in_the_update(installed, tmp_path):
    before_accounts(installed)
    rules = [
        ("Password of the administrator", ["short", SECRETS["web_password"]]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(installed, tmp_path, rules, argv=["--update"])
    assert code == 0, term.text
    assert len(admin_runs(runner)) == 1


def test_a_failing_admin_script_stops_the_update_before_the_rebuild(installed, tmp_path):
    before_accounts(installed)
    runner = FakeRunner(installed)
    runner.fail("set_admin.py", 1)
    rules = [
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, _, _ = play(installed, tmp_path, rules, argv=["--update"], runner=runner)
    assert code == 1
    assert not runner.ran("docker compose up")


def test_a_dry_run_update_asks_for_no_password(installed, tmp_path):
    before_accounts(installed)
    code, term, _ = play(installed, tmp_path, [], argv=["--update", "--dry-run"])
    assert code == 0, term.text
    assert "becomes the first" in term.text


def test_the_checks_name_the_missing_admin_when_the_hash_is_still_open(installed, tmp_path):
    """A resumed or repaired installation without an admin asks for the password."""
    before_accounts(installed)
    rules = [
        ("1 = Change settings", "3"),
        ("Password of the administrator", SECRETS["web_password"]),
        ("Repeat the password", SECRETS["web_password"]),
    ]
    code, term, runner = play(installed, tmp_path, rules)
    assert code == 0, term.text
    assert len(admin_runs(runner)) == 1
