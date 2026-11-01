# Killswitch test procedure (issue #31, built on #4 and #24)

The one promise that must never break: **qBittorrent cannot reach the internet except through
the VPN.** This page says how to prove it, when, and what a failure means. Two scripts do the
proving; both really break the VPN for a short while, so run them when downloads can pause.

## Why it holds (the layers)

1. qBittorrent shares gluetun's network namespace and has no network of its own.
2. gluetun's firewall blocks everything that is not the tunnel (plus the LAN, so the UI answers).
3. qBittorrent is bound to the `tun0` interface: no tunnel, no interface, no connections.
4. The watchdog notices a dropped VPN within seconds and pauses all torrents ([watchdog.md](watchdog.md)).
5. A slow egress check compares the public exit address with the home line and alerts or pauses
   on a confirmed mismatch.

The tests below prove 1 to 4 against the real stack. (5 has its own unit tests and a check in
the abuse test's recovery step.)

## When to run what

| When | Run | Time |
|---|---|---|
| after the first start, and after any change to the VPN, firewall, compose network or image tags | `make killswitch-test` | 2 to 5 min |
| after changing the watchdog, `WATCHDOG_*` settings, gluetun or qBittorrent versions | `make vpn-abuse-test` | about 2 min per cycle |
| now and then (for example every few months) | both; with `CYCLES=20` for the abuse test | |

Both need the stack up and healthy (`docker compose ps`), run on the media VM, and print **only
verdicts, never an address**, so the output can be shared.

## 1. The killswitch test: `make killswitch-test`

```bash
make killswitch-test         # = scripts/vpn/killswitch-test.sh   (--dry-run lists the steps)
```

It stops gluetun on purpose and always starts it again, even if the test fails.

| Step | What it does | Pass |
|---|---|---|
| 1 | asks a "what is my IP" service from inside the qBittorrent container | an address that is **not** the home one: `VPN IP` |
| 2 | `docker stop gluetun`, then asks again | `no connection`. If it answers with the home address that is a `HOME IP LEAK` and the setup is wrong |
| 3 | `docker start gluetun`, waits for it to be healthy | qBittorrent gets its connection back **by itself** with a VPN address again |

It passes when the output ends with `OK: the killswitch holds and qbittorrent recovers`. Exit code
0 = passed, 1 = a step failed, 2 = the stack was not ready (nothing was changed).

Step 3 is the recovery path: when gluetun restarts as the same container it gets a new network
namespace, and qBittorrent would keep the dead one. A watcher inside the qBittorrent container
(`scripts/qbittorrent/services/netwatch`) notices that the interface is gone and restarts that
container. If recovery ever fails, `docker restart qbittorrent` does it by hand. After a `git pull`
that changes the watcher, restart qBittorrent too.

## 2. The abuse test: `make vpn-abuse-test`

```bash
make vpn-abuse-test CYCLES=20      # = scripts/vpn/vpn-abuse-test.sh --cycles 20
scripts/vpn/vpn-abuse-test.sh --cycles 4 --modes vpn --dry-run   # a plan, changes nothing
```

Needs the watchdog running and **at least one torrent running** in qBittorrent (add a small
legal one, for example a Linux ISO; `--allow-no-torrent` skips that check). Each cycle breaks the
VPN in one of four ways, rotating: `stop`, `kill` (SIGKILL), `restart`, and `vpn` (only the OpenVPN
process dies, the container stays up so the pause can be sent). Per cycle it checks:

1. the watchdog notices (state `down`) in time;
2. **no leak**: a probe inside qBittorrent's network runs every half second while the VPN is
   down; it must never see the home address, and must see `no connection` at least once;
3. in `vpn` mode all torrents are paused in time;
4. recovery: gluetun healthy, the watchdog `ok`, a VPN address again;
5. resume behaviour matches `WATCHDOG_RESUME_AFTER`.

One PASS/FAIL line per check and a table at the end. Exit 0 = every check of every cycle passed.
It refuses to start unless the stack is healthy, always starts gluetun again and resumes the
torrents it paused.

## If a test fails

| Symptom | Likely cause | What to do |
|---|---|---|
| `HOME IP LEAK` | the network setup is wrong (qBittorrent not in gluetun's namespace, firewall rule changed) | **stop downloading**, `docker compose stop qbittorrent`, compare `docker-compose.yml` with the repository, fix, re-run |
| step 3 never recovers | netwatch did not restart qBittorrent | `docker restart qbittorrent`; check `docker compose logs qbittorrent` for netwatch lines; `docker compose ps` shows qBittorrent `unhealthy` while its namespace is dead |
| exit 2, "stack not ready" | gluetun or the watchdog is not healthy | `docker compose ps`; the VPN credentials or `make vpn-config` are the usual cause ([host-setup.md](host-setup.md)) |
| abuse test: watchdog never `down` | no read-only key, or `QBIT_PASSWORD` empty | `make watchdog-key`, set `QBIT_PASSWORD` in `.env`, `docker compose up -d --force-recreate gluetun watchdog` |
| abuse test: `no connection` never seen in a cycle | the outage was shorter than the probe interval, so the window was not really observed | lower `AB_PROBE_INTERVAL` (default 0.5 s); the probe is `curl` run inside the qBittorrent container |

Settings of both scripts (all optional) are environment variables listed in
[configuration.md](configuration.md#variables-of-the-test-and-ops-scripts).
