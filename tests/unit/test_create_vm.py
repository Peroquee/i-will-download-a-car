"""scripts/proxmox/create-vm.sh in --dry-run mode: VM sizing and boot order from the issue."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.shell import run_script

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "proxmox" / "create-vm.sh"
ISO = "local:iso/debian-12.12.0-amd64-netinst.iso"


def create(*args: str):
    return run_script(SCRIPT, ["--dry-run", *args])


def qm_create_line(result) -> str:
    return next(line for line in result.stdout.splitlines() if line.startswith("+ qm create"))


def test_vm_matches_issue_sizing():
    line = qm_create_line(create("--vmid", "210", "--iso", ISO))
    assert "--cores 4" in line
    assert "--memory 4096" in line
    assert "local-lvm:32," in line
    assert "--balloon 0" in line, "ballooning would let Proxmox take RAM from the VPN stack"


def test_vm_is_a_full_vm_with_guest_agent_and_starts_first():
    line = qm_create_line(create("--vmid", "210", "--iso", ISO))
    assert "--agent enabled=1" in line
    assert "--onboot 1" in line
    assert "--startup order=1" in line


def test_custom_storage_and_bridge():
    line = qm_create_line(
        create("--vmid", "7", "--iso", ISO, "--storage", "tank", "--bridge", "vmbr1")
    )
    assert "tank:32," in line
    assert "bridge=vmbr1" in line


def test_media_disk_is_optional_and_excluded_from_backups():
    without = create("--vmid", "210", "--iso", ISO)
    assert "--scsi1" not in without.stdout
    with_disk = create(
        "--vmid", "210", "--iso", ISO, "--media-size", "500", "--media-storage", "bulk"
    )
    line = next(x for x in with_disk.stdout.splitlines() if "--scsi1" in x)
    assert "bulk:500," in line
    assert "backup=0" in line


@pytest.mark.parametrize(
    "args",
    [
        ["--iso", ISO],
        ["--vmid", "abc", "--iso", ISO],
        ["--vmid", "210"],
        ["--vmid", "210", "--iso", ISO, "--media-size", "big"],
        ["--vmid", "210", "--iso", ISO, "--nope"],
    ],
)
def test_invalid_arguments(args):
    result = create(*args)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
