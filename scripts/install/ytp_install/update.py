"""Updating an existing installation: `./install.sh --update` and `./update.sh`.

In this order, and it stops at the first failure with the old version still running:

1. a **backup** of the database, config and .env (`scripts/ops/backup.py create`): without a
   backup nothing is changed;
2. `git pull --ff-only` (as the person who owns the folder, not as root);
3. new settings of the template are added to `.env` with their defaults (otherwise a new required
   setting would stop `docker compose`); nothing the owner set is touched;
4. the VPN file is rendered again (server addresses rotate);
4b. an installation from before the accounts (#44) gets its first admin: the password is asked once
   (the web login's name is kept) and written as a scrypt hash (`scripts/app/set_admin.py`);
5. `docker compose up -d --build` and the wait until every service is healthy;
6. qBittorrent's categories and the Web UI settings of the proxy are applied again through its
   API (a qBittorrent.conf from before the proxy lacks them; this needs no stop of qBittorrent);
7. the host checks.
"""

from __future__ import annotations

import os

from . import answers as ans
from .envfile import apply_settings, missing_settings, parse_env, write_env
from .steps import (
    LATER_SETTINGS,
    PLACEHOLDER,
    Context,
    StepError,
    admin_hash_set,
    set_admin,
    step_qbit,
    wait_healthy,
)


def sync_env_defaults(ctx: Context) -> list[str]:
    """Add the template's new settings to .env; returns their names."""
    example = (ctx.root / ".env.example").read_text(encoding="utf-8")
    current = ctx.env_path.read_text(encoding="utf-8")
    added = missing_settings(example, current)
    if not added:
        return []
    text = apply_settings(current, added)
    ctx.say(ctx.tr("env.added", names=", ".join(sorted(added))))
    # settings the installer fills in itself right after (the admin password is asked next)
    placeholders = sorted(
        k for k, v in added.items() if v == PLACEHOLDER and k not in LATER_SETTINGS
    )
    if placeholders:
        ctx.say(ctx.tr("env.placeholder", names=", ".join(placeholders)))
    if ctx.dry_run:
        ctx.say("+ add the new settings to .env (the old file is backed up first)")
        return sorted(added)
    backup = write_env(ctx.env_path, text, ctx.now())
    if backup is not None:
        ctx.say(ctx.tr("backup.made", name=backup.name))
        ctx.hand_back(backup)
    ctx.hand_back(ctx.env_path)
    return sorted(added)


def ensure_admin(ctx: Context, conv) -> None:
    """An installation from before the accounts (#44) has no admin. Its web login becomes the
    first admin: the same name (PROXY_USER), a password asked once (the old one is only a bcrypt
    hash and cannot be reused)."""
    env = parse_env(ctx.env_path.read_text(encoding="utf-8"))
    if admin_hash_set(env):
        return
    user = env.get("PROXY_USER", "")
    if ans.check_user(user) is not None:
        user = "admin"
    ctx.say("")
    ctx.say(ctx.tr("update.admin", user=user))
    password = ctx.answers.web_password
    if not password and not ctx.dry_run:
        password = conv.secret(
            "update.admin_password.ask", ans.check_web_password, twice=True, n=ans.MIN_WEB_PASSWORD
        )
    if ctx.dry_run:
        ctx.say("+ make the first admin account of the app (asks for a password)")
        return
    ctx.runner.register_secrets([password])
    set_admin(ctx, user, password)
    ctx.hand_back(ctx.env_path)


def run_update(ctx: Context, state, conv) -> int:
    say = ctx.say
    say("")
    say(ctx.tr("update.start"))
    runner = ctx.runner

    say(ctx.tr("update.backup"))
    if not runner.run([ctx.script("ops", "backup.py"), "create"], stream=True).ok:
        say(ctx.tr("update.backup_failed"))
        return 1

    if (ctx.root / ".git").exists():
        say(ctx.tr("update.pull"))
        pull = ["git", "pull", "--ff-only"]
        if os.geteuid() == 0 and ctx.login.name != "root":
            pull = ["runuser", "-u", ctx.login.name, "--", *pull]
        if not runner.run(pull, stream=True).ok:
            say(ctx.tr("update.pull_failed"))
            return 1

    try:
        sync_env_defaults(ctx)
        ensure_admin(ctx, conv)
        if (
            parse_env(ctx.env_path.read_text(encoding="utf-8")).get("VPN_SERVICE_PROVIDER")
            == "custom"
            and (ctx.root / "config" / "expressvpn.ovpn").exists()
        ):
            runner.run([ctx.script("vpn", "render_ovpn.py")])
        if not runner.run(["docker", "compose", "up", "-d", "--build"], stream=True).ok:
            raise StepError("step.start.hint")
        wait_healthy(ctx)
        if ctx.answers.qbit_password:
            # categories and the Web UI settings of the proxy, through its API (idempotent)
            step_qbit(ctx)
        if not runner.run(
            [ctx.script("host", "check-host.sh"), "--env-file", ".env"], stream=True
        ).ok:
            raise StepError("step.checks.hint")
    except StepError as error:
        say("")
        say(ctx.tr(error.hint))
        if error.detail and error.detail != error.hint:
            say("  " + error.detail.splitlines()[-1][:300])
        return 1
    say(ctx.tr("update.done"))
    return 0
