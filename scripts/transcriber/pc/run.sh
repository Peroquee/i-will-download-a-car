#!/usr/bin/env bash
# run.sh: start the Whisper worker with the CUDA libraries of its virtual environment.
#
# faster-whisper (CTranslate2) needs cuBLAS and cuDNN 9 for CUDA 12. They come as pip packages
# into the worker's own venv (install.sh), so the netboot image needs only the NVIDIA driver, no
# system CUDA toolkit. The dynamic loader has to be told where they are before Python starts.
set -euo pipefail

PREFIX="${YTP_TRANSCRIBER_PREFIX:-/opt/ytp-transcriber}"
PYTHON="$PREFIX/venv/bin/python"

libs="$("$PYTHON" - <<'PY' 2>/dev/null || true
import os
import nvidia.cublas.lib
import nvidia.cudnn.lib
print(os.path.dirname(nvidia.cublas.lib.__file__) + ":" + os.path.dirname(nvidia.cudnn.lib.__file__))
PY
)"
if [[ -n "$libs" ]]; then
    export LD_LIBRARY_PATH="$libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$PYTHON" "$PREFIX/worker.py" "$@"
