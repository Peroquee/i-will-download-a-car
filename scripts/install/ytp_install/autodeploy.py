"""Setting up automatic updates (issue #42), after the numbered steps.

Not one of the eight steps: it is optional, it must never make a finished installation look
broken, and the steps' bookkeeping (state file, "step 3 of 8") stays as it was. What it does when
the answers say `autodeploy_remote`:

1. the read-only GitLab token goes to `config/autodeploy/token` (folder 700, file 600, owned by
   the person who runs the installer); it is never put into `.env` or on a command line. Without a
   new token an existing file is kept;
2. `scripts/ops/autodeploy.py install-units --user <login>` writes the systemd timer and service
   from the repository's templates and starts the timer (the installer runs as root);
3. `scripts/ops/autodeploy.py check` runs as the login user: can this machine do it (token valid,
   GitLab and git reachable, docker usable, backup folder fine)?

With no remote, nothing happens, except that a timer set up earlier is removed when the person
switches the feature off in "change settings".
"""

from __future__ import annotations

import os

from .envfile import write_private
from .steps import Context, StepError


def setup_autodeploy(ctx: Context) -> None:
    answers = ctx.answers
    script = ctx.script("ops", "autodeploy.py")
    if not answers.autodeploy_remote:
        if ctx.previous is not None and ctx.previous.autodeploy_remote:
            ctx.runner.run([script, "remove-units"])
            ctx.say(ctx.tr("autodeploy.off"))
        return
    ctx.say("")
    ctx.say(ctx.tr("autodeploy.title"))
    folder = ctx.root / "config" / "autodeploy"
    token = folder / "token"
    if answers.autodeploy_token:
        if ctx.dry_run:
            ctx.say("+ write config/autodeploy/token (mode 600)")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            write_private(token, answers.autodeploy_token + "\n")
            ctx.hand_back(folder, token)
            ctx.say(ctx.tr("autodeploy.token_saved"))
    elif not token.exists() and not ctx.dry_run:
        raise StepError("autodeploy.token_missing")
    ctx.hand_back(ctx.root / "config")
    if not ctx.runner.run([script, "install-units", "--user", ctx.login.name]).ok:
        raise StepError("step.autodeploy.hint")
    check = [script, "check"]
    if os.geteuid() == 0 and ctx.login.name != "root":
        check = ["runuser", "-u", ctx.login.name, "--", *check]
    result = ctx.runner.run(check, stream=True)
    if not result.ok:
        ctx.say(ctx.tr("autodeploy.check_failed", user=ctx.login.name))
        raise StepError("step.autodeploy.hint")
    ctx.say(ctx.tr("autodeploy.on"))
