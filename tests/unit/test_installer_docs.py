"""README, the two AI-help files, docs/installer.md and the installer stay in step (issue #37)."""

# ruff: noqa: E501

from __future__ import annotations

import re

import pytest
from ytp_install import cli
from ytp_install.answers import Answers
from ytp_install.steps import STEP_IDS
from ytp_install.texts import TEXTS

from tests.helpers.installer import ROOT

README = (ROOT / "README.md").read_text(encoding="utf-8")
AI_EN = (ROOT / "AI-HELP.md").read_text(encoding="utf-8")
AI_DE = (ROOT / "HILFE-MIT-KI.md").read_text(encoding="utf-8")
INSTALLER_DOC = (ROOT / "docs" / "installer.md").read_text(encoding="utf-8")
INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
REPO_URL = re.search(r"YTP_REPO_URL:-([^}]+)\}", INSTALL_SH).group(1)  # type: ignore[union-attr]


def all_settings() -> set[str]:
    """Every setting the installer can write, whatever the VPN choice."""
    names: set[str] = set()
    base = dict(lan_address="192.168.178.20", lan_subnet="192.168.178.0/24")
    for extra in (
        {},
        dict(vpn_kind="builtin", vpn_provider="mullvad", vpn_type="wireguard"),
        dict(vpn_kind="builtin", vpn_provider="mullvad", vpn_type="openvpn"),
    ):
        names |= set(Answers(**base, **extra).env_values())
    return names


def options() -> set[str]:
    parser_options: set[str] = set()
    import argparse

    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        for action in self._actions:
            parser_options.update(
                o for o in action.option_strings if o.startswith("--") and o != "--help"
            )
        return original(self, ["--lang", "en"], namespace)

    argparse.ArgumentParser.parse_args = capture  # type: ignore[method-assign]
    try:
        cli.parse_args([])
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    return parser_options


# --- the README -------------------------------------------------------------------------------


def test_the_readme_has_a_what_is_this_and_five_steps_in_both_languages():
    assert "## What is this? / Was ist das?" in README
    assert "**English.**" in README
    assert "**Deutsch.**" in README
    section = README.split("## Installation in 5 steps / Installation in 5 Schritten", 1)[1].split(
        "\n## ", 1
    )[0]
    numbers = re.findall(r"^(\d)\. \*\*", section, re.M)
    assert numbers == ["1", "2", "3", "4", "5"]
    for german in (
        "Ein Computer mit Debian 12",
        "Deine VPN-Zugangsdaten",
        "Herunterladen und starten",
        "Fragen beantworten",
        "Webseite öffnen",
    ):
        assert german in section


def test_the_readme_start_command_matches_install_sh():
    assert f"git clone {REPO_URL} youtube-premium" in README
    assert "./install.sh" in README
    assert README.count("github.com/Peroquee") == 1


def test_the_readme_points_to_the_help_files_and_the_details():
    for target in ("AI-HELP.md", "HILFE-MIT-KI.md", "docs/installer.md", "docs/host-setup.md"):
        assert f"]({target})" in README, target
        assert (ROOT / target).is_file()


def test_the_readme_lists_the_new_files_in_its_layout():
    for name in ("docs/installer.md", "install.sh, update.sh", "AI-HELP.md", "HILFE-MIT-KI.md"):
        assert name in README


# --- the two help files -------------------------------------------------------------------------


def numbered_steps(text: str) -> list[str]:
    block = text.split("\n## ", 20)
    section = next(
        b for b in block if b.startswith(("The installer's steps", "Die Schritte des Installers"))
    )
    return re.findall(r"^\d\. \*\*(.+?)\*\*", section, re.M)


def test_the_english_help_lists_the_installers_steps_with_their_exact_titles():
    assert numbered_steps(AI_EN) == [TEXTS[f"step.{s}.title"]["en"] for s in STEP_IDS]


def test_the_german_help_lists_the_installers_steps_with_their_exact_titles():
    assert numbered_steps(AI_DE) == [TEXTS[f"step.{s}.title"]["de"] for s in STEP_IDS]


@pytest.mark.parametrize("text", [AI_EN, AI_DE], ids=["en", "de"])
def test_every_setting_the_installer_writes_is_in_the_help_table(text):
    missing = {name for name in all_settings() if f"`{name}`" not in text}
    # fixed values the person is never asked for
    missing -= {"OPENVPN_CUSTOM_CONFIG"} if "`OPENVPN_CUSTOM_CONFIG`" in text else set()
    assert not missing, sorted(missing)


@pytest.mark.parametrize("text", [AI_EN, AI_DE], ids=["en", "de"])
def test_the_help_names_every_installer_option(text):
    for option in options():
        assert option in text or option == "--answers", option


@pytest.mark.parametrize("text", [AI_EN, AI_DE], ids=["en", "de"])
def test_the_help_forbids_pasting_secrets(text):
    lowered = text.lower()
    for needle in (".env", ".ovpn", "api"):
        assert needle in lowered
    assert "config/installer.log" in text
    assert "./install.sh" in text
    assert "./update.sh" in text


@pytest.mark.parametrize("text", [AI_EN, AI_DE], ids=["en", "de"])
def test_the_help_starts_with_the_start_commands_of_the_readme(text):
    assert f"git clone {REPO_URL} youtube-premium" in text


@pytest.mark.parametrize("text", [AI_EN, AI_DE], ids=["en", "de"])
def test_the_docs_the_help_points_to_exist(text):
    for name in set(re.findall(r"`((?:docs/)[a-z-]+\.md)`", text)) | {"README.md"}:
        assert (ROOT / name).is_file(), name
    for name in set(re.findall(r"`([A-Za-z-]+\.md)`", text)):
        assert (ROOT / name).is_file() or (ROOT / "docs" / name).is_file(), name


def test_both_help_files_cover_the_same_ground():
    def refs(text):
        return set(re.findall(r"`(docs/[a-z-]+\.md)`", text))

    assert refs(AI_EN) == refs(AI_DE)
    assert len(re.findall(r"^\| ", AI_EN, re.M)) == len(re.findall(r"^\| ", AI_DE, re.M))
    assert len(re.findall(r"^- \*\*", AI_EN, re.M)) == len(re.findall(r"^- \*\*", AI_DE, re.M))
    assert AI_EN.count("\n## ") == AI_DE.count("\n## ")


def test_the_help_files_link_to_each_other():
    assert "(HILFE-MIT-KI.md)" in AI_EN
    assert "(AI-HELP.md)" in AI_DE


# --- install.sh and docs/installer.md -------------------------------------------------------------


def test_install_sh_documents_every_option_the_program_has():
    for option in options():
        assert option in INSTALL_SH, option


def test_the_installer_doc_names_every_option_and_step():
    for option in options():
        assert option in INSTALLER_DOC, option
    for step_id in STEP_IDS:
        assert TEXTS[f"step.{step_id}.title"]["en"] in INSTALLER_DOC, step_id


def test_the_installer_doc_names_the_files_it_writes():
    for name in ("config/installer-state.json", "config/installer.log", ".env.bak-"):
        assert name in INSTALLER_DOC


# --- no personal data --------------------------------------------------------------------------------


def test_no_personal_address_or_e_mail_is_in_the_new_files():
    files = [
        ROOT / "README.md", ROOT / "AI-HELP.md", ROOT / "HILFE-MIT-KI.md",
        ROOT / "docs" / "installer.md", ROOT / "install.sh", ROOT / "update.sh",
        *(ROOT / "scripts" / "install").rglob("*.py"),
    ]  # fmt: skip
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text), (
            f"an e-mail address in {path.name}"
        )
        for match in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
            first = int(match.split(".")[0])
            assert first in {10, 127, 172, 192, 0, 1, 8}, (
                f"{match} in {path.name}: only private example addresses belong here"
            )
