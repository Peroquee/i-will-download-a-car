"""The installer's part of automatic updates (issue #42): the question, the token file, the timer."""

# ruff: noqa: E501

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from ytp_install import answers as ans
from ytp_install import cli
from ytp_install.envfile import parse_env

from tests.helpers.installer import (
    FakeRunner,
    FakeTerminal,
    deps,
    make_repo,
    new_install_rules,
)

pytestmark = pytest.mark.unit

REMOTE = "http://gitlab.lan/web-projects/youtube-premium.git"
TOKEN = "glpat-install-test-123"  # a test value, low entropy on purpose


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def ovpn(tmp_path) -> Path:
    path = tmp_path / "my_vpn.ovpn"
    path.write_text("client\ndev tun\nremote vpn.example.net 1195\n")
    return path


def play(repo, tmp_path, rules, argv=(), **overrides):
    term = FakeTerminal(rules)
    runner = FakeRunner(repo, dry_run="--dry-run" in argv)
    code = cli.main(["--lang", "en", *argv], repo, deps(term, runner, tmp_path, **overrides))
    return code, term, runner


def with_autodeploy(ovpn, remote=REMOTE, token=TOKEN):
    return [
        *new_install_rules(ovpn),
        ("Switch on automatic updates", "y"),
        ("Address of the repository", remote),
        ("Access token", token),
    ]


# --- the answers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "http://gitlab.lan/web-projects/youtube-premium.git",
        "https://gitlab.example.org/group/sub/project",
        "http://192.168.178.194:8080/g/p.git",
    ],
)
def test_a_repository_address_is_accepted(value):
    assert ans.check_git_remote(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "gitlab.lan/g/p.git",
        "ssh://git@gitlab.lan/g/p.git",
        "http://user:pw@gitlab.lan/g/p.git",
        "http://user@gitlab.lan/g/p.git",
        "http://gitlab.lan/project.git",
        "http://gitlab.lan/g/p.git?x=1",
        "http://gitlab.lan/g/p space.git",
        "http://gitlab.lan/g/p.git#frag",
        "file:///srv/repo.git",
    ],
)
def test_a_bad_repository_address_is_refused(value):
    assert ans.check_git_remote(value) == "err.remote"


@pytest.mark.parametrize("value", [TOKEN, "A" * 8, "x" * 200, "a.b_c-d="])
def test_a_token_shape_is_accepted(value):
    assert ans.check_gitlab_token(value) is None


@pytest.mark.parametrize(
    "value", ["", "short", "x" * 201, "has space token", "bad/char/token1", "über-token-123"]
)
def test_something_that_is_not_a_token_is_refused(value):
    assert ans.check_gitlab_token(value) == "err.token"


def test_the_remote_is_a_setting_but_the_token_never_is():
    a = ans.Answers(autodeploy_remote=REMOTE, autodeploy_token=TOKEN)
    values = a.env_values()
    assert values["AUTODEPLOY_REMOTE"] == REMOTE
    assert TOKEN not in "".join(values.values())
    assert ans.Answers.from_env({"AUTODEPLOY_REMOTE": REMOTE}).autodeploy_remote == REMOTE
    assert ans.Answers().env_values()["AUTODEPLOY_REMOTE"] == ""


# --- the conversation --------------------------------------------------------------------------


def test_by_default_there_are_no_automatic_updates(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, new_install_rules(ovpn))
    assert code == 0, term.text
    assert parse_env((repo / ".env").read_text())["AUTODEPLOY_REMOTE"] == ""
    assert not (repo / "config" / "autodeploy").exists()
    assert not runner.ran("autodeploy.py")
    assert any("Switch on automatic updates" in line for line in term.transcript), "it asks"


def test_saying_yes_stores_the_token_privately_installs_the_timer_and_checks(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, with_autodeploy(ovpn))
    assert code == 0, term.text
    env = parse_env((repo / ".env").read_text())
    assert env["AUTODEPLOY_REMOTE"] == REMOTE
    assert TOKEN not in (repo / ".env").read_text(), "the token is not in .env"
    token = repo / "config" / "autodeploy" / "token"
    assert token.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    install = runner.ran("autodeploy.py install-units")[0]
    assert install[install.index("--user") + 1] == "pat"
    assert runner.index("autodeploy.py install-units") < runner.index("autodeploy.py check")
    assert runner.index("docker compose up -d --build") < runner.index(
        "autodeploy.py install-units"
    ), "after the stack runs"
    assert "Automatic updates are on" in term.text


def test_the_token_is_never_on_a_command_line_or_in_the_log(repo, tmp_path, ovpn):
    _, term, runner = play(repo, tmp_path, with_autodeploy(ovpn))
    assert all(TOKEN not in " ".join(c) for c in runner.commands)
    assert all(TOKEN not in line for line in runner.lines)
    assert TOKEN not in term.text
    assert TOKEN in term.typed_secrets, "typed hidden"


def test_the_default_address_is_offered_and_a_bad_one_is_asked_again(repo, tmp_path, ovpn):
    rules = [
        *new_install_rules(ovpn),
        ("Switch on automatic updates", "y"),
        ("Address of the repository", ["http://user:pw@gitlab.lan/g/p.git", ""]),
        ("Access token", TOKEN),
    ]
    code, term, _ = play(repo, tmp_path, rules)
    assert code == 0, term.text
    assert "without a user name or password" in term.text
    assert parse_env((repo / ".env").read_text())["AUTODEPLOY_REMOTE"] == ans.DEFAULT_REMOTE


def test_a_bad_token_is_asked_again(repo, tmp_path, ovpn):
    rules = [
        *new_install_rules(ovpn),
        ("Switch on automatic updates", "y"),
        ("Address of the repository", REMOTE),
        ("Access token", ["has space", TOKEN]),
    ]
    code, term, _ = play(repo, tmp_path, rules)
    assert code == 0, term.text
    assert "does not look like a GitLab token" in term.text
    assert (repo / "config" / "autodeploy" / "token").read_text() == TOKEN + "\n"


def test_a_failing_check_does_not_make_the_installation_look_broken_but_exits_3(
    repo, tmp_path, ovpn
):
    runner = FakeRunner(repo)
    runner.fail("autodeploy.py check", 1, "FAIL  GitLab refused the token")
    term = FakeTerminal(with_autodeploy(ovpn))
    code = cli.main(["--lang", "en"], repo, deps(term, runner, tmp_path))
    assert code == 3
    assert "Automatic updates could not be set up" in term.text
    assert (repo / ".env").exists()
    assert "Done!" in term.text, "the rest is finished and the summary still shows"


def test_a_failing_timer_installation_is_reported_the_same_way(repo, tmp_path, ovpn):
    runner = FakeRunner(repo)
    runner.fail("install-units", 1, "boom")
    term = FakeTerminal(with_autodeploy(ovpn))
    code = cli.main(["--lang", "en"], repo, deps(term, runner, tmp_path))
    assert code == 3
    assert not runner.ran("autodeploy.py check"), "no check without a timer"


def test_without_a_token_anywhere_it_stops_with_the_way_forward(repo, tmp_path, ovpn):
    answers = tmp_path / "answers"
    answers.write_text(
        "web_password=web-web-web-2x\nvpn_user=vpn-user\nvpn_password=vpn-vpn-vpn-1\n"
        f"ovpn_source={ovpn}\nautodeploy_remote={REMOTE}\n"
    )
    answers.chmod(0o600)
    term = FakeTerminal()
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "en", "--answers", str(answers)], repo, deps(term, runner, tmp_path))
    assert code == 3
    assert "There is no token" in term.text
    assert not runner.ran("install-units")


def test_an_answers_file_can_carry_remote_and_token(repo, tmp_path, ovpn):
    answers = tmp_path / "answers"
    answers.write_text(
        "web_password=web-web-web-2x\nvpn_user=vpn-user\nvpn_password=vpn-vpn-vpn-1\n"
        f"ovpn_source={ovpn}\nautodeploy_remote={REMOTE}\nautodeploy_token={TOKEN}\n"
    )
    answers.chmod(0o600)
    term = FakeTerminal()
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "en", "--answers", str(answers)], repo, deps(term, runner, tmp_path))
    assert code == 0, term.text
    assert (repo / "config" / "autodeploy" / "token").read_text() == TOKEN + "\n"
    assert runner.ran("autodeploy.py install-units")


def test_an_answers_file_with_a_bad_remote_is_refused_before_anything_runs(repo, tmp_path, ovpn):
    answers = tmp_path / "answers"
    answers.write_text(
        "web_password=web-web-web-2x\nvpn_user=vpn-user\nvpn_password=vpn-vpn-vpn-1\n"
        f"ovpn_source={ovpn}\nautodeploy_remote=http://u:p@gitlab.lan/g/p.git\n"
    )
    answers.chmod(0o600)
    term = FakeTerminal()
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "en", "--answers", str(answers)], repo, deps(term, runner, tmp_path))
    assert code == 2
    assert not runner.commands


def test_a_dry_run_writes_no_token_and_runs_nothing(repo, tmp_path, ovpn):
    code, term, runner = play(repo, tmp_path, with_autodeploy(ovpn), argv=["--dry-run"])
    assert code == 0, term.text
    assert not (repo / "config" / "autodeploy").exists()
    assert not (repo / ".env").exists()
    assert any("autodeploy.py install-units" in line for line in runner.lines), "it shows the plan"


def test_changing_settings_to_no_removes_the_timer(repo, tmp_path, ovpn):
    play(repo, tmp_path, with_autodeploy(ovpn))
    rules = [
        ("1 = Change settings", "1"),
        ("Path of the .ovpn file", ""),
        ("Change the web page password", "n"),
        ("Switch on automatic updates", "n"),
    ]
    term = FakeTerminal(rules)
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "en"], repo, deps(term, runner, tmp_path))
    assert code == 0, term.text
    assert parse_env((repo / ".env").read_text())["AUTODEPLOY_REMOTE"] == ""
    assert runner.ran("autodeploy.py remove-units")
    assert "switched off" in term.text
    assert (repo / "config" / "autodeploy" / "token").exists(), (
        "the token file is left to the owner"
    )


def test_changing_other_settings_keeps_the_remote_as_the_default(repo, tmp_path, ovpn):
    play(repo, tmp_path, with_autodeploy(ovpn))
    rules = [
        ("1 = Change settings", "1"),
        ("Path of the .ovpn file", ""),
        ("Change the web page password", "n"),
        ("Switch on automatic updates", "y"),
        ("Address of the repository", ""),
        ("Access token", ""),
    ]
    term = FakeTerminal(rules)
    runner = FakeRunner(repo)
    code = cli.main(["--lang", "en"], repo, deps(term, runner, tmp_path))
    assert code == 0, term.text
    assert parse_env((repo / ".env").read_text())["AUTODEPLOY_REMOTE"] == REMOTE
    assert (repo / "config" / "autodeploy" / "token").read_text() == TOKEN + "\n", "kept"
    assert not runner.ran("remove-units")


def test_the_german_texts_exist_for_everything_it_says():
    from ytp_install.texts import TEXTS

    for key in TEXTS:
        if "autodeploy" in key or key in {"err.remote", "err.token"}:
            assert TEXTS[key]["de"].strip()
            assert TEXTS[key]["de"] != TEXTS[key]["en"]
