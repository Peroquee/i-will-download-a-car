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


def test_the_german_conversation(repo, tmp_path, ovpn):
    rules = [(n, a) for n, a in new_install_rules(ovpn)]
    rules[1:] = [
        ("Wie bekommst du die Zugangsdaten", "1"),
        ("Pfad der .ovpn-Datei", str(ovpn)),
        ("VPN-Benutzername", "vpn-user"),
        ("VPN-Passwort", SECRETS["vpn_password"]),
        ("Passwort für die Webseite", SECRETS["web_password"]),
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
        ("private key", "wg-private-key-SECRET-9"),
        ("your address in the VPN", "10.64.222.21/32"),
        ("Country of the VPN server", "Germany"),
        ("Password for the web page", SECRETS["web_password"]),
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
    assert env["WIREGUARD_PRIVATE_KEY"] == "wg-private-key-SECRET-9"
    assert env["OPENVPN_CUSTOM_CONFIG"] == ""
    assert not runner.ran("render_ovpn.py"), "a built-in provider needs no file"
    assert "wg-private-key-SECRET-9" not in whole_output(term, runner)


def test_a_wrong_answer_is_explained_and_asked_again(repo, tmp_path, ovpn):
    rules = new_install_rules(ovpn)
    rules = [(n, a) for n, a in rules if n != "Password for the web page"]
    rules.append(
        ("Password for the web page", ["short", "has'quote-longenough", SECRETS["web_password"]])
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
    assert "wrong VPN credentials" in term.text
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
        ("Password for the web page", SECRETS["web_password"]),
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
        ("User name for the web page", "pat"),
        ("Change the web page password", "n"),
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
        ("Change the web page password", "n"),
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
