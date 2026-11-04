# Subtitles and Whisper transcription

Every YouTube video should have subtitles in Jellyfin. The app gets them in two ways:

1. **From YouTube**, when the video has them: fetched with the video and filed next to it.
2. **From Whisper**, for every video that has none: a PC with a GPU (the RTX 2080 Super) is
   woken over the network, boots from the network into Linux, transcribes everything that waits,
   and switches itself off again.

```
 app (media VM)                         wake.py (media VM, timer)           main PC (netboot Linux)
 ───────────────                        ─────────────────────────           ───────────────────────
 download video
 ├─ YouTube subtitles? ── yes ─> <video>.de.srt  (done)
 └─ no ─> transcriptions: queued ──────> queued and PC not up? ── WoL ──>   boots from the network
                                                                            ytp-transcriber.service
          /api/transcriber/claim  <──────────────────────────────────────── worker asks for work
          /api/transcriber/jobs/1/media ───── video (streamed) ──────────>  ffmpeg -> 16 kHz WAV
          /api/transcriber/jobs/1/heartbeat <────────────────────────────── faster-whisper on CUDA
          /api/transcriber/jobs/1/result  <──────────── SRT ─────────────── 
 <video>.<lang>.srt, Jellyfin refresh
                                                                            queue empty for 5 min:
                                                                            exit 77 -> poweroff
```

## 1. Subtitles from YouTube

With every video (target `video`, and podcast episodes kept as video) the app looks at which
subtitles YouTube lists. If one exists in a language of `YOUTUBE_SUBTITLE_LANGS` (default `de,en`),
a second, separate yt-dlp pass fetches exactly those tracks, converts them to SRT and they are
filed next to the video:

```
Youtube/<channel>/<title> [<id>].mp4
Youtube/<channel>/<title> [<id>].de.srt      <- Jellyfin shows "German" in the subtitle menu
Youtube/<channel>/<title> [<id>].en.srt
```

- One file per language; `de-DE` and `de` both become `.de.srt` (the plain one wins).
- Only subtitles a **person** made, by default. YouTube's automatic captions are taken only with
  `YOUTUBE_AUTO_SUBTITLES=1`, and then only in the video's own language: YouTube also lists
  machine translations of them into every other language, which are never taken. Whisper is
  usually better than YouTube's captions, so the default leaves those videos to Whisper.
- A subtitle that cannot be fetched never fails the video (it goes to Whisper instead). A video
  without subtitles costs no extra request: the first pass already says which ones exist.
- Audio-only downloads (podcast episodes, music) get no subtitles and no transcription.
- `YOUTUBE_SUBTITLE_LANGS=` (set, but empty) switches subtitles off.

## 2. The transcription queue in the app

A video that is filed without any subtitle next to it (`<stem>.*.srt/.ass/.vtt`) gets a row in the
table `transcriptions` (migration 0014), **if** `TRANSCRIBER_TOKEN` is set. The download job is
`done` either way: the video can be watched at once, the subtitle follows.

| Status | Meaning |
|---|---|
| `queued` | waiting for the PC |
| `running` | the PC has it (an attempt; the PC sends a heartbeat every 30 s) |
| `done` | the SRT is filed (or there was no speech: no file) |
| `failed` | 3 attempts failed, or the video was deleted meanwhile. An alert says so on the dashboard. Downloading the video again queues it again. |

A PC that stops sending heartbeats for 10 minutes (crash, power cut, someone switched it off) loses
the video back to the queue; that counts as an attempt.

**The PC's routes** (`ytp/transcriber_api.py`) are not behind the login. They need the header
`X-Transcriber-Token: <TRANSCRIBER_TOKEN>` (not `Authorization`: the proxy removes that one) and
answer 404 while `TRANSCRIBER_TOKEN` is empty. The video itself is fetched with a per-attempt
random key in the link instead of the token (so ffmpeg can read it with range requests, and the
token never appears on a command line); the key works only for that video and only while that
attempt runs. The PC never names a path: it receives an id and sends text, and the app checks the
SRT (cue times, size, no NUL), writes it to a temporary name and renames it, never over an existing
subtitle.

```bash
# from a LAN machine, to look at the queue
curl -s -H "X-Transcriber-Token: $TOKEN" http://<vm>:<PROXY_PORT>/api/transcriber/status
# {"queued": 2, "running": 0, "done": 31, "failed": 0, "pending_downloads": 1, "worker_last_seen": "..."}
```

## 3. Waking the PC: `scripts/transcriber/wake.py` (media VM)

A systemd timer runs `wake.py run` every 2 minutes. It reads the app's database (read only) and
sends a Wake-on-LAN packet when videos are queued, the PC's worker has not asked for work in the
last 3 minutes, no packet went out in the last 10 minutes (booting takes a while), and the hour is
inside `TRANSCRIBER_WAKE_HOURS` (if set). It never shuts anything down.

Set up, on the media VM, in the install folder:

```bash
# 1. the settings in .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # -> TRANSCRIBER_TOKEN
#    TRANSCRIBER_TOKEN=<that value>
#    TRANSCRIBER_PC_MAC=<MAC of the PC's network card that boots from the network>
#    TRANSCRIBER_WOL_ADDRESS=192.168.178.255     (the broadcast address of the PC's network)
#    TRANSCRIBER_WAKE_HOURS=                      (optional, e.g. 1-7)
sudo docker compose up -d app                  # the app reads TRANSCRIBER_TOKEN

# 2. test the packet by hand (the PC must be off, or asleep)
python3 scripts/transcriber/wake.py wake
python3 scripts/transcriber/wake.py status

# 3. the timer (as root; runs as your user, who owns config/)
sudo python3 scripts/transcriber/wake.py install-units --user "$USER"
systemctl list-timers youtube-premium-transcriber-wake.timer
```

The VM must be in the same layer-2 network (the same bridge/VLAN) as the PC's network card, or the
broadcast does not reach it. If the PC is linked to the server through its own switch, give the VM
a network card on that bridge, or use that network's broadcast address.

## 4. The PC: netboot Linux with the worker

**What the PC must do on its own** (the netboot setup, not part of this repository):

- **Wake-on-LAN** on in the BIOS/UEFI (often "Power On By PCI-E", "Wake on LAN") and, for a PC
  that was shut down from Windows, in Windows too: device manager -> network card -> "Wake on
  Magic Packet" on, and **Fast Startup off** (otherwise Windows leaves the card without standby
  power on many boards).
- **Boot from the network when woken by the packet**, and from the disk (Windows) otherwise. Many
  boards have a "Wake on LAN boot device" / "Boot from onboard LAN" option for exactly this; else
  put network boot first with a short timeout and let the netboot menu default to the local disk
  unless the server has work. The netboot server (the LXC with dnsmasq, TFTP and NFS, the signed
  shim + GRUB chain from Ubuntu) then hands it the Linux image.
- **The NVIDIA driver in the image**, matching the image's kernel. With Secure Boot on, use
  Ubuntu's signed prebuilt modules (`linux-modules-nvidia-<version>-generic`), not DKMS, or the
  module will not load. `nvidia-smi` must list the RTX 2080 Super after boot.

**Installing the worker into the image** (once; again after an update of these files): copy the
folder `scripts/transcriber/pc/` into the image and run, as root inside it (a chroot on the netboot
server with `/proc`, `/sys`, `/dev` bound in, or the booted PC if its root is writable):

```bash
sudo ./install.sh
sudo nano /etc/ytp-transcriber.env    # YTP_APP_URL and TRANSCRIBER_TOKEN
```

It installs `python3-venv` and `ffmpeg`, a system user `ytp-transcriber`, a virtual environment
in `/opt/ytp-transcriber` with `faster-whisper` and the CUDA 12 libraries it needs (cuBLAS,
cuDNN 9, no system CUDA toolkit), and enables `ytp-transcriber.service`.

| Setting (`/etc/ytp-transcriber.env`) | Default | Meaning |
|---|---|---|
| `YTP_APP_URL` | (required) | The app as the PC reaches it: `http://<PROXY_BIND>:<PROXY_PORT>`. The PC must be in `LAN_SUBNET`. |
| `TRANSCRIBER_TOKEN` | (required) | The same value as in the app's `.env`. |
| `WHISPER_MODEL` | `large-v3-turbo` | Nearly the quality of `large-v3` at several times the speed. `large-v3` for hard German audio, `medium` for less VRAM. |
| `WHISPER_COMPUTE_TYPE` | `int8_float16` | About 2-3 GB of VRAM. `float16` is a little better and needs about twice as much. |
| `WHISPER_MODEL_DIR` | `/var/lib/ytp-transcriber/...` | Where the model is kept (1.6 GB for turbo). On a read-only or RAM-backed image, a folder on the server's NVMe over NFS, writable by `ytp-transcriber`, so it is downloaded once and not at every boot. |
| `WORK_DIR` | `/tmp` | Where the sound is unpacked: about 115 MB per hour of video. |
| `IDLE_SHUTDOWN_SECONDS` | `300` | Switch off after this long with nothing queued or running. |
| `WAIT_FOR_DOWNLOADS_SECONDS` | `1800` | While YouTube downloads are still in progress (they may need Whisper), stay up this long at most. |
| `UNREACHABLE_SHUTDOWN_SECONDS` | `900` | Switch off when the app has not answered for this long. |
| `SHUTDOWN_WHEN_IDLE` | `1` | `0` = never switch off. |
| `NETBOOT_ONLY` | `1` | Only switch off when `/proc/cmdline` shows a network boot (`NETBOOT_MARKERS`, default `nfsroot=,root=/dev/nfs,netboot=`): the worker on a normal installation never powers anything off. |
| `BUSY_COMMAND` | empty | A command that exits 0 while the PC is busy with something else, e.g. `pgrep -x aider` for the coding agent on the same image. |

**When does it switch off?** The worker exits with code 77 and the unit's `ExecStopPost`
(`poweroff-if-done.sh`, root) powers off, only for 77. It stays on while someone is logged in
(`who`), while `/run/ytp-transcriber-keep-awake` exists, while `BUSY_COMMAND` says busy, and when
the system was not booted from the network. Stopping the service by hand never powers off.

**Whisper settings that matter** (fixed in `worker.py`): the voice-activity filter is on (silence
and music are skipped, which is where Whisper invents text), `condition_on_previous_text` is off
(no repetition loops), and segments that are exactly one of Whisper's well-known inventions
("Untertitel im Auftrag des ZDF ...", "Thanks for watching!") are dropped. If YouTube states the
video's language, it is passed to Whisper; otherwise Whisper detects it from the first 30 seconds,
and the file is named with the language Whisper heard.

**GPU memory.** If the coding model (Ollama with `qwen3-coder:30b`) is loaded on the same GPU at
the same time, there is not enough VRAM for both. The attempt then fails with "the GPU ran out of
memory" and is tried again later (3 attempts). Set `BUSY_COMMAND` so the PC stays up for the coding
agent, and let Ollama unload its model when idle (`OLLAMA_KEEP_ALIVE`).

## Troubleshooting

| What you see | Where to look |
|---|---|
| Videos stay `queued`, the PC does not start | `python3 scripts/transcriber/wake.py status` on the VM (why it does or does not wake); `journalctl -u youtube-premium-transcriber-wake`. Test the packet with `wake.py wake` while the PC is off. |
| The PC starts, but into Windows | The board does not boot from the network on a WoL wake: see "Boot from the network when woken" above. |
| The PC boots the netboot Linux but nothing happens | On the PC: `journalctl -u ytp-transcriber -b`. "the app is not reachable": `YTP_APP_URL`, and is the PC in `LAN_SUBNET`? "answered 401": the token differs. |
| "could not load library libcudnn..." | The venv lacks the CUDA libraries: run `install.sh` again. `nvidia-smi` must work first. |
| The PC never switches off | `journalctl -u ytp-transcriber` says why ("staying on: someone is logged in", "not booted from the network", ...). |
| Wrong language in the file name | YouTube stated a wrong language for the video. Rename the file (`.en.srt` -> `.de.srt`) and refresh the library in Jellyfin. |
| `failed` with "the video is not in the library any more" | The video was deleted or renamed before the PC got to it. Nothing to do. |

## Updating

- yt-dlp is the app's (docs/youtube.md). The PC's `faster-whisper` is pinned in `install.sh`;
  bump it there and run `install.sh` in the image again.
- The worker files (`worker.py`, `run.sh`, `poweroff-if-done.sh`, the unit) are copied by
  `install.sh`; after an update of this repository, copy the folder into the image and run it
  again. `/etc/ytp-transcriber.env` is kept.
