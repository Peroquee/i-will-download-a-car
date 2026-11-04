#!/usr/bin/env bash
# poweroff-if-done.sh: ExecStopPost of ytp-transcriber.service: switch the PC off when the
# worker said "nothing left to do" (exit code 77), and only then.
#
# systemd sets EXIT_STATUS for ExecStopPost. Stopping the service by hand (TERM), a crash (any
# other code) or a reboot never powers anything off. Runs as root (the `+` in the unit).
set -euo pipefail

if [[ "${EXIT_STATUS:-}" == "77" ]]; then
    echo "ytp-transcriber: nothing left to transcribe, switching off"
    systemctl --no-block poweroff
fi
