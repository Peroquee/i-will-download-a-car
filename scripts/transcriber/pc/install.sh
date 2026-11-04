#!/usr/bin/env bash
# install.sh: install the Whisper worker into the Linux the transcription PC boots from the network.
#
# Run as root INSIDE that system: on the booted PC if its root file system is writable, or in a
# chroot of the netboot image on the netboot server (with /proc, /sys and /dev bound in, and the
# image's /etc/resolv.conf working). Safe to run again (an update keeps /etc/ytp-transcriber.env).
#
#   sudo ./install.sh            install, enable the service
#   sudo ./install.sh --no-enable   install only (start it yourself: systemctl enable --now ytp-transcriber)
#
# What it does: apt packages (python3-venv, ffmpeg), a system user `ytp-transcriber`, a virtual
# environment in /opt/ytp-transcriber with faster-whisper and the CUDA 12 libraries it needs
# (cuBLAS, cuDNN 9: no system CUDA toolkit needed), the worker and its unit. It does NOT install the
# NVIDIA driver: that belongs to the image (it must match the image's kernel), see
# docs/transcription.md.
set -euo pipefail

PREFIX=/opt/ytp-transcriber
ENV_FILE=/etc/ytp-transcriber.env
UNIT=/etc/systemd/system/ytp-transcriber.service
SERVICE_USER=ytp-transcriber
# Pinned like everything else in this project; bump on purpose (docs/transcription.md).
FASTER_WHISPER=faster-whisper==1.1.1
CUDA_LIBS=(nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*")
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

enable=1
for arg in "$@"; do
    case "$arg" in
        --no-enable) enable=0 ;;
        -h | --help)
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option $arg" >&2
            exit 2
            ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "install.sh: run me as root (sudo ./install.sh)" >&2
    exit 1
fi
if ! command -v apt-get >/dev/null; then
    echo "install.sh: this needs a Debian or Ubuntu system (apt-get)" >&2
    exit 1
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends python3 python3-venv ffmpeg procps

if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    echo "== GPU: $(nvidia-smi -L | head -n 1)"
else
    echo "!! nvidia-smi does not see a GPU here. In a chroot that is expected; on the PC the image"
    echo "!! needs the NVIDIA driver for its kernel (Ubuntu: ubuntu-drivers install, or the signed"
    echo "!! linux-modules-nvidia-*-generic package when Secure Boot is on). See docs/transcription.md."
fi

echo "== user $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/ytp-transcriber --no-create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "== $PREFIX"
install -d -m 755 "$PREFIX"
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q "$FASTER_WHISPER" "${CUDA_LIBS[@]}"
install -m 755 "$HERE/worker.py" "$PREFIX/worker.py"
install -m 755 "$HERE/run.sh" "$PREFIX/run.sh"
install -m 755 "$HERE/poweroff-if-done.sh" "$PREFIX/poweroff-if-done.sh"

echo "== $ENV_FILE"
if [[ ! -e "$ENV_FILE" ]]; then
    install -m 640 -o root -g "$SERVICE_USER" "$HERE/transcriber.env.example" "$ENV_FILE"
    echo "   written from the example: set YTP_APP_URL and TRANSCRIBER_TOKEN in it"
else
    chown root:"$SERVICE_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    echo "   kept as it is"
fi

echo "== $UNIT"
install -m 644 "$HERE/ytp-transcriber.service" "$UNIT"
systemctl daemon-reload 2>/dev/null || true # not possible in a chroot; the next boot reads it
if [[ "$enable" -eq 1 ]]; then
    systemctl enable ytp-transcriber.service
fi

if grep -q '^TRANSCRIBER_TOKEN=changeme' "$ENV_FILE"; then
    echo
    echo "Next: edit $ENV_FILE (YTP_APP_URL, TRANSCRIBER_TOKEN), then boot the PC from the network."
fi
echo "done."
