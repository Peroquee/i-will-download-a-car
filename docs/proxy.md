# Reverse proxy and login (issue #29)

The app can trigger downloads, and qBittorrent's Web UI can do worse, so neither is
reachable directly. **Caddy** is the only way in from the LAN. It does two things before it
passes a request on, in this order:

1. **LAN check.** The client's address must be inside `LAN_SUBNET` (default `192.168.178.0/24`),
   otherwise Caddy answers `403` and the request goes no further.
2. **Login.** HTTP basic auth: `401` unless user and password match.

```
LAN client ──► VM:80    ─► Caddy ─► app:8000        (over the compose bridge)
           ──► VM:8081  ─► Caddy ─► gluetun:8080    (qBittorrent's Web UI, in gluetun's namespace)

not published:  app 8000 · qBittorrent 8080 on the LAN (gluetun publishes it on 127.0.0.1 only)
```

Plain HTTP on the LAN is intended, as for the GitLab: anyone who can sniff your LAN could read
the login. That is the accepted risk of a trusted home network; the safeguards are the LAN
check, a long password and no port forwarding on the router (the line is DS-Lite anyway). If
you ever want TLS, Caddy can do it with its internal CA; nothing here requires it.

## Setup

1. **Address.** In `.env` set `PROXY_BIND` to the VM's own LAN address (`hostname -I`). The
   default example is `192.168.178.123`. It must be an IPv4 address of this machine: Docker
   then publishes the proxy on that address only, and Compose refuses to start if the address
   does not exist. `PROXY_PORT` (default 80) is the app, `PROXY_QBT_PORT` (default 8081) the
   qBittorrent Web UI.
2. **Login.** `make proxy-password` asks for a password (twice, at least 12 characters), has
   Caddy hash it with bcrypt and writes `PROXY_USER` and `PROXY_PASSWORD_HASH` to `.env`. The
   password itself is never written, logged or put on a command line. `--user NAME` changes the
   user (default `admin`). Run it again any time to change the password, then
   `docker compose up -d`.
3. **Start.** `docker compose up -d` (pulls the pinned Caddy image, starts the proxy after the
   app is healthy). Compose stops with a message when `PROXY_USER` or `PROXY_PASSWORD_HASH` is
   missing, and the placeholder `changeme` from `.env.example` matches no password, so the
   proxy refuses everyone until step 2 has been done.
4. **Check** from a PC on the LAN (needs only Python 3 and a copy of `.env`; or from the VM):

   ```bash
   scripts/proxy/verify_proxy.py                   # reads address, ports and user from .env
   scripts/proxy/verify_proxy.py --host 192.168.178.123    # when PROXY_BIND is 0.0.0.0
   ```

   It asks for the password and prints PASS/FAIL per check: `401` without a login (with a
   `WWW-Authenticate: Basic` challenge) and with a wrong password, `200` with the right one
   on both ports, and the app's own port 8000 and qBittorrent's own port not reachable on the
   VM's LAN address. Exit code 0 means all passed.

Then open `http://<vm>/` for the app and `http://<vm>:8081/` for qBittorrent (browser login
first, then qBittorrent's own login, which stays on as a second layer).

## Design and decisions

**The LAN check uses the connection's own source address (`remote_ip`), never a header.**
`client_ip` would honour `X-Forwarded-For` as soon as `trusted_proxies` is set, so neither is
used and a test (`tests/unit/test_proxy_config.py`) fails if either word appears in the
Caddyfile. A client that sends `X-Forwarded-For: 192.168.178.5` from outside the LAN is still
judged by its real address. The check comes **first**, so an outside client learns nothing, not
even that a login exists.

**What counts as the LAN.** Only `LAN_SUBNET`. Not "any private address", so the Docker bridge
(`172.x`, where `app` and `gluetun` live) is *not* inside it: another container, or anything
that reaches the proxy through the bridge, is refused like an outsider. The VM's own LAN address
is inside the LAN. A request to `127.0.0.1` on the VM arrives from Docker's gateway address
(`172.x`) and gets a `403`; use the VM's LAN address instead (or run the check from a PC).
Keep `LAN_SUBNET` as narrow as your network: if it were a /8 the check would mean little.

**Several layers, none of them the only one.** (1) The router has no port forwarding. (2)
`PROXY_BIND` publishes on the VM's LAN address only (IPv4 on purpose: the IPv6 mapping of Docker
hides the client's address). (3) Caddy's LAN check. (4) The login. (5) The app is published
nowhere and qBittorrent's Web UI only on `127.0.0.1` (`QBIT_WEBUI_BIND` stays as it is, the
maintenance scripts on the VM use it). (6) qBittorrent's own login behind the proxy.

**qBittorrent behind a proxy.** It gets its own port (`8081`) instead of a path like `/qbt/`:
its Web UI uses absolute paths and a cookie scoped to `/`, so a sub-path needs rewriting that
cannot be tested without the real UI. Caddy passes the `Host` header through unchanged, which is
what qBittorrent's CSRF check needs (it compares `Origin` with `Host` and answers `401` when the
port differs; that is what we saw with a port mapping). `ReverseProxySupportEnabled` is
**not** switched on: it would make qBittorrent believe `X-Forwarded-For` from a "trusted proxy
list" whose address is a changing Docker address. The consequence: qBittorrent sees every client
as the proxy. Five wrong *qBittorrent* passwords (after passing the proxy login) ban the proxy
for an hour, which locks you out of the Web UI until `docker restart qbittorrent`. Attackers
cannot cause that: they never get past the proxy login to reach qBittorrent's login.

**The container.** Runs as `PUID:PGID`, read-only root, `tmpfs` for `/data`, `/config`, `/tmp`
(no certificates or state to keep), no capabilities except `NET_BIND_SERVICE`, which the
official image's binary needs to start (it listens on 8080/8081, not on privileged ports),
`no-new-privileges`, no admin API (`admin off`), no Docker socket, no access to the media. Its
config is mounted read-only as a folder (`./proxy`). The version is pinned (`CADDY_TAG`).

**Fail closed.** The Caddyfile reads `LAN_SUBNET`, `PROXY_USER` and `PROXY_PASSWORD_HASH` from
the environment; Compose requires all three; an empty value makes Caddy refuse to start or
refuse every request. Nothing is served half-configured.

**Passwords and brute force.** bcrypt (cost 14) makes every guess slow, but Caddy has no
rate limit of its own. The login is only reachable from the LAN. Use a long password. The hash
is in `.env` (mode 600, gitignored) and is visible with `docker compose config` and
`docker inspect`: it is a hash, not the password, but treat those outputs as private.
Caddy's access log (JSON, `docker compose logs proxy`) redacts the `Authorization` header.

**The hash and `$`.** A bcrypt hash contains `$`, which Compose would read as a variable
(`$2a` ...). `make proxy-password` writes it in single quotes (`PROXY_PASSWORD_HASH='$2a$14$...'`),
which Compose keeps literally. If you ever edit it by hand, keep the quotes.

## What is tested, and where

| What | Where | Needs |
| --- | --- | --- |
| Compose service: pinned image, user, read-only, capabilities, ports bound to `PROXY_BIND`, app unpublished, required login | `tests/unit/test_proxy_config.py`, `tests/integration/test_compose_config.py` | nothing / Compose CLI (CI) |
| Caddyfile decisions: `remote_ip` before `basic_auth`, no `client_ip`/`trusted_proxies`, no admin API, listeners = published ports | `tests/unit/test_proxy_config.py` | nothing (CI) |
| `set_password.py`: hash via stdin, quoting, idempotent, mode kept, bad input | `tests/unit/test_proxy_set_password.py` | nothing (CI) |
| `verify_proxy.py` against a fake proxy | `tests/unit/test_proxy_verify.py` | nothing (CI) |
| The real Caddyfile in a real Caddy: valid, 401/200 inside, **403 outside including spoofed `X-Forwarded-For`**, refused requests never reach the upstream, fail closed | `tests/system/test_c10_proxy.py` | Docker daemon (`make test-system` on the VM, or the ytp-host runner) |
| The running stack from the LAN | `make proxy-check` | the stack running |

The outside-the-LAN refusal is shown by the system test, not by `make proxy-check`: one machine
cannot send from two source addresses. The test starts a throwaway Caddy with the same image
and Caddyfile on its own Docker network and sets `LAN_SUBNET` to a network the test client is
not in.

## Troubleshooting

- **`401` with the right password:** the hash was not written (still `changeme`), or it lost its
  quotes. Run `make proxy-password` again, then `docker compose up -d`.
- **`403`:** the client is not in `LAN_SUBNET`. From the VM itself use its LAN address, not
  `127.0.0.1`; if even that gives `403`, test from a PC.
- **`docker compose up` says "cannot assign requested address":** `PROXY_BIND` is not an address
  of this VM.
- **`502` on port 8081:** the proxy cannot reach `gluetun:8080`. Is gluetun healthy and
  qBittorrent running (`docker compose ps`)? If gluetun's firewall drops traffic from the Docker
  bridge, add `FIREWALL_INPUT_PORTS: "8080"` to gluetun's environment and tell us: the stack
  did not need it when this was written, but it could not be tried without the VM.
- **qBittorrent answers `401` through the proxy:** the browser reached it on another port than
  it was asked for (a port mapping in between) or you are using a second reverse proxy.
- **qBittorrent answers `403` for everyone:** a ban after wrong qBittorrent passwords; see
  above, `docker restart qbittorrent`.
