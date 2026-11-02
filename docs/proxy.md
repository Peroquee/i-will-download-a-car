# Reverse proxy and login (issue #29)

The app can trigger downloads, and qBittorrent's Web UI can do worse, so neither is
reachable directly. **Caddy** is the only way in from the LAN. Before it passes a request on:

1. **LAN check (both ports).** The client's address must be inside `LAN_SUBNET` (default
   `192.168.178.0/24`), otherwise Caddy answers `403` and the request goes no further.
2. **Login, depending on the port.**
   - The **app port** (`PROXY_PORT`) has no proxy login since #44: the app has its own accounts,
     sessions and permissions and answers `401` itself ([accounts.md](accounts.md)). Caddy only
     keeps the LAN check there.
   - The **qBittorrent port** (`PROXY_QBT_PORT`) keeps HTTP basic auth (`PROXY_USER` /
     `PROXY_PASSWORD_HASH`): `401` unless user and password match. That is the admin's door;
     qBittorrent has its own login behind it.

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
2. **Logins.** The app's first admin: `make admin-password` (scrypt hash in `APP_ADMIN_USER` /
   `APP_ADMIN_PASSWORD_HASH`, see [accounts.md](accounts.md)). The qBittorrent port:
   `make proxy-password` asks for a password (twice, at least 12 characters), has
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

   It asks for the proxy password (the qBittorrent port's) and prints PASS/FAIL per check: the
   app answers `/health` with `200` through the proxy and `401` (from the app, **without** a
   `WWW-Authenticate: Basic` challenge) on `/api/jobs`; the qBittorrent port answers `401`
   without a login and `200` with it; the app's own port 8000 and qBittorrent's own port are not
   reachable on the VM's LAN address. Exit code 0 means all passed.

Then open `http://<vm>/` for the app (the app's login page) and `http://<vm>:8081/` for
qBittorrent (browser login first, then qBittorrent's own login, which stays on as a second layer).

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
cannot be tested without the real UI. Caddy passes the `Host` header through unchanged (`<lan address>:8081`), and qBittorrent's
CSRF check, which compares `Origin` with `Host`, is **on** and keeps working.

**The proxy login is not passed on.** The browser sends the basic-auth login (`Authorization:
Basic ...`) with every request to the qBittorrent port; Caddy checks it and, by default, would
forward it. (The app port has no basic auth any more, but keeps the rule: whatever a client puts
in that header never reaches the app, and the app does not use it either; its login is the
session cookie.) Both
`reverse_proxy` blocks therefore remove the header (`header_up -Authorization`): no backend has a
use for the proxy password, and qBittorrent 5.3 answers `401` to **any** request that carries an
Authorization header (it reads one as API-key authentication), whatever the `Host` header says.
This, and not only the Host header below, was the cause of the `401` the first installs got. The
system test `test_c10_proxy.py` checks with a stand-in upstream that echoes its request headers
that none arrives; `test_c1_qbittorrent_login.py` shows the `401` of the real image.

**Host header validation is off (`WebUI\HostHeaderValidation=false`).** qBittorrent answers
`401` to any request whose `Host` header names another port than its own Web UI port (8080), or
an address it does not know. Through the proxy the port is always 8081, so with the check on the
Web UI could never be reached that way (found in the first real install: the proxy check
`qBittorrent with the login -> 200` got 401; `curl -H "Host: <lan address>:8081"
http://127.0.0.1:8080/` gave 401, `Host: 127.0.0.1:8080` gave 200). `seed_config.py` therefore
writes `WebUI\HostHeaderValidation=false` and, explicitly, `WebUI\CSRFProtection=true` into a new config.

Why this is acceptable: the check protects a Web UI that is open on a network against DNS
rebinding (a hostile site's name that resolves to the Web UI's address). Here nothing reaches
port 8080 except (1) Caddy, which refuses anything outside `LAN_SUBNET` and then asks for the
proxy login, and (2) localhost on the VM (`QBIT_WEBUI_BIND`, the maintenance scripts). Both are
unaffected by a rebinding page: Caddy's LAN check and the login come first, qBittorrent's own
login and CSRF protection come after, and the Web UI is not published anywhere else. So the
check would only add a way for the owner's own proxy to break.

**Existing installations.** A `qBittorrent.conf` made before this change lacks the setting, and a
running qBittorrent rewrites its config file, so the file is not edited. Instead
`scripts/qbittorrent/setup_categories.py` sets the preferences `web_ui_host_header_validation_enabled`
(false) and `web_ui_csrf_protection_enabled` (true) through qBittorrent's Web API, like the
categories; qBittorrent applies them at once and stores them itself, so they survive restarts.
The installer runs that script after the stack is up on a new install, on *Repair*, on *Change
settings* and on *Update*. By hand, from the install folder:

```bash
scripts/qbittorrent/setup_categories.py --user admin     # asks for the qBittorrent password
```

`ReverseProxySupportEnabled` is
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
refuse every request. Nothing is served half-configured. (The app port needs only the LAN
subnet, but the proxy still starts as one piece: without a qBittorrent login it does not start.)
The app itself fails closed too: with no account nobody gets past the login page, and the app
logs an error that says how to make the admin.

**Passwords and brute force.** bcrypt (cost 14) makes every guess slow, but Caddy has no
rate limit of its own, so the qBittorrent port is protected by the LAN check and a long password.
(The app brakes guessing itself: [accounts.md](accounts.md).) The hash
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
| Caddyfile decisions: `remote_ip` before `basic_auth`, basic auth on the qBittorrent port only, no `client_ip`/`trusted_proxies`, no admin API, listeners = published ports | `tests/unit/test_proxy_config.py` | nothing (CI) |
| `set_password.py`: hash via stdin, quoting, idempotent, mode kept, bad input | `tests/unit/test_proxy_set_password.py` | nothing (CI) |
| `verify_proxy.py` against a fake proxy | `tests/unit/test_proxy_verify.py` | nothing (CI) |
| The real Caddyfile in a real Caddy: valid, qBittorrent port 401/200 inside and the app port passing requests on, **403 outside on both ports including spoofed `X-Forwarded-For`**, refused requests never reach the upstream, fail closed | `tests/system/test_c10_proxy.py` | Docker daemon (`make test-system` on the VM, or the ytp-host runner) |
| The running stack from the LAN | `make proxy-check` | the stack running |

The outside-the-LAN refusal is shown by the system test, not by `make proxy-check`: one machine
cannot send from two source addresses. The test starts a throwaway Caddy with the same image
and Caddyfile on its own Docker network and sets `LAN_SUBNET` to a network the test client is
not in.

## Troubleshooting

- **`401` on the app page with the right password:** that is the app's login, not the proxy's:
  see [accounts.md](accounts.md) (lost password: `make admin-password`, then restart the app).
- **`401` on port 8081 with the right password:** the hash was not written (still `changeme`), or
  it lost its quotes. Run `make proxy-password` again, then `docker compose up -d`.
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
