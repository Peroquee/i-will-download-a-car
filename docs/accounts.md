# Accounts, login and permissions (issue #44)

The app has its own accounts. Caddy only checks that a client is inside the LAN (and, on the
qBittorrent port, asks for the proxy login); everything on the app port is judged by the app
([proxy.md](proxy.md)).

## Roles

| | admin | user |
| --- | --- | --- |
| Submit downloads (magnet, YouTube, podcast episode) | yes | yes |
| Subscribe to channels and podcasts, "check now" on their own | yes | yes |
| See jobs | all of them | only their own |
| See subscriptions | all, with the names of the followers | only the ones they follow |
| Dashboard | everything (VPN history, disk, update status, alerts) | their own jobs, the VPN state (no exit country) |
| Alerts | yes | no |
| Manage accounts (create, role, disable, reset password, delete) | yes | no |
| qBittorrent Web UI (port 8081, proxy login) | yes | not their business: they have no login for it |
| Change their own password | yes | yes |

The library is shared: `Youtube/<channel>/...` is the same folder for everyone.

## One subscription, several followers

Subscribing to a channel (or podcast) that somebody already follows does **not** create a second
subscription: the asker becomes another **follower** of the same one, so nothing is downloaded
twice. A video the subscription queues belongs to all of its followers (they see the job; the
admin sees every job). Unfollowing (`DELETE /api/subscriptions/{id}`) removes only the asker; the
subscription ends when the last follower leaves. An admin can end a subscription for everybody
with `?everyone=1`. Somebody else's subscription or job looks exactly like one that does not
exist (`404`, never `403`), so nobody learns what others follow.

A download started by hand (magnet or YouTube link) belongs to the person who started it; if
another person submits the same thing while it is in the system, they become a co-owner of that
one job instead of getting an error or a second download.

## First admin, new installs and updates

`make admin-password` (`scripts/app/set_admin.py`) asks for a password (twice, at least 12
characters) and writes `APP_ADMIN_USER` (default `admin`, `--user NAME` changes it) and
`APP_ADMIN_PASSWORD_HASH` to `.env`; run `docker compose up -d` afterwards. The hash is scrypt
(`n=2**15, r=8, p=1`, about 80 ms and 32 MiB per check) in one line `scrypt:...` without a `$`,
so Compose does not mistake it for a variable. The password is never written, logged or put on a
command line (`--password-stdin` and `--dry-run` exist).

At start the app creates the admin account from these two settings if it does not exist, and
gives it everything that has no owner yet: the subscriptions and jobs of an installation from
before accounts existed belong to the first admin. The migration (`0010_accounts.sql`) only adds
tables, so the previous version still runs on the new database (see the update rule in
[app.md](app.md)).

**Lost admin password.** Run `make admin-password` again and restart the app: a hash that is
different from the one applied last time **resets the password** of that account, makes it an
active admin again and ends its sessions. If the hash is unchanged nothing happens, so a password
changed in the app stays changed. Other users' passwords are reset by an admin in the app.

## Updating an existing installation (one step)

The installer's update (`./update.sh`) does it for you: it asks **once** for a password, keeps
your old login name as the admin's name and writes the hash. If the owner updates by hand
(`git pull`) or the automatic update (#42) installs the new version, nothing can ask a question,
so the app starts without an admin and:

* the page shows, instead of a dead login form: *"The administrator account has not been set up
  yet ... run `make admin-password`, then `docker compose up -d`"* (German too);
* the app logs an error with the same instruction, `scripts/host/check-host.sh` fails its `login`
  check, and the automatic update raises its usual "updated, but the host checks report problems"
  alert.

The one step, on the server in the install folder (it asks for the password twice):

```bash
make admin-password          # writes APP_ADMIN_USER / APP_ADMIN_PASSWORD_HASH to .env (mode kept)
docker compose up -d         # starts the app again with the new setting
```

Use `--user NAME` (`scripts/app/set_admin.py --user NAME`) to name the admin; the default is
`admin`. Everything that existed before (subscriptions, jobs) belongs to this first admin. The
qBittorrent port (8081) keeps its old login (`PROXY_USER` / `PROXY_PASSWORD_HASH`).

## Sessions

- Login: `POST /api/auth/login` `{username, password}`. The answer sets the cookie
  `ytp_session` (**HttpOnly**, **SameSite=Strict**, `Path=/`; no `Secure` because the LAN uses
  plain HTTP on purpose). It holds 256 random bits; the database keeps only their SHA-256, so a
  copy of the database cannot be used to log in.
- **No session fixation:** every login makes a new token and ends the one that came with the
  request; a token chosen by somebody else is never adopted.
- A session ends after 14 days without use or 30 days at most, at logout
  (`POST /api/auth/logout`: also on the server, so a copied cookie is dead), when the account is
  disabled or deleted, and when its password is changed or reset (a user who changes their own
  password keeps the session they did it from). A change of role applies at the next request.
- **CSRF:** the cookie is SameSite=Strict, and every POST/PUT/PATCH/DELETE also passes the
  same-origin check described in [app.md](app.md) (`Origin: null` and other sites get `403`).
- Wrong name, wrong password, unknown name and disabled account all answer the same
  `401 wrong name or password`, and an unknown name costs the same time as a wrong password.

## The brake on guessing

Caddy has none, so the app counts failures: 5 within 15 minutes for one name (not case
sensitive) lock that name, 30 for one client address lock the address. The lock starts at 30
seconds and doubles with every further failure, up to 15 minutes; during it even the right
password answers `429` with `Retry-After`. A success clears the name's count. Counting is in
memory (a restart clears it). The client address is the last entry of `X-Forwarded-For` as Caddy
sets it, believed only when the connection itself comes from a private address (the proxy on the
compose network); a header from anywhere else is ignored. Changing your own password also needs
the current one and is braked the same way.

## Permissions are default-deny

`app/ytp/auth.py` has one table, `POLICY`: for every route who may call it (public, any
logged-in account, admin). A route that is not in the table is **admin only**, a path that no
route serves needs a login before it says `404`, and `tests/unit/test_auth_policy.py` fails when a
route has no entry or an entry names no route. Public are only: the page and its files, `/health`,
`/api/version`, `/reload-needed`, the login and `/api/auth/me` (which answers `401` itself).

## Passwords and logs

Request bodies of the auth routes are read by hand, not through the validation that quotes bad
input in its answer, so a password never comes back in an error. No password, hash or session
token is logged (`tests/unit/test_auth_api.py` checks the log of a whole session); a login is
logged as `login` / `login failed` with the account id and the client address.

## API

| Route | Who | |
| --- | --- | --- |
| `POST /api/auth/login` | anyone | `{username, password}` → `200 {account}` + cookie, `401`, `422`, `429` |
| `POST /api/auth/logout` | logged in | ends this session |
| `GET /api/auth/me` | anyone | `200 {account}` or `401` |
| `POST /api/auth/password` | logged in | `{current, new}`; `403` if `current` is wrong |
| `GET /api/accounts` | admin | list |
| `POST /api/accounts` | admin | `{username, password, role?}` → `201`; `409` exists; `422` |
| `PATCH /api/accounts/{id}` | admin | `{role?, disabled?}` |
| `POST /api/accounts/{id}/password` | admin | `{password}` |
| `DELETE /api/accounts/{id}` | admin | removes its follows; a channel nobody follows ends; its jobs stay |

There must always be one active admin: demoting, disabling or deleting the last one answers
`409 last_admin`. A name is 3 to 64 characters of letters, digits, `.`, `_`, `-`, not starting
with a symbol, and is unique regardless of case.

## In the browser

* **Not signed in:** the page shows only the sign-in form (script `static/auth.js`; the body
  carries `data-auth` = `unknown` / `out` / `in` and `data-role`, and the style sheet hides what a
  visitor or a user may not use). Nothing asks the app for data before the sign-in: a visitor's
  page makes no request except "who am I" (`/api/auth/me`) and the version check. A wrong password
  says so, too many tries say how long to wait. After a good sign-in the page reloads and starts as
  that account. If the session ends while a page is open (`401` on any request) the form comes back
  with a note. Without any account the form is replaced by the set-up message above.
* **Account button** (top right, shows the name): a dialog with *Change password* (current, new,
  repeated; other devices are signed out) and *Sign out*.
* **Users** see *Input*, *Dashboard*, *Subscriptions* and *How to*; the dashboard has no disk, VPN
  history, update status or alerts for them. **Admins** also get *Accounts* (`/accounts`): create an
  account, make admin / user, disable, set a password, delete (two clicks), with the last-admin
  rule explained. A user who opens `/accounts` is sent back to the start page.
* **Subscriptions** show how many others follow the same channel (admins see the names, and a
  *Remove for everyone* button). Subscribing to a channel somebody follows says so.

## Tests

| What | Where |
| --- | --- |
| Every route's permission as anonymous / user / admin; no route without an entry; default-deny; bad cookies | `tests/unit/test_auth_policy.py` |
| Login, cookie flags, fixation, logout, password change, account management, last admin, the brake, CSRF, no passwords in logs | `tests/unit/test_auth_api.py` |
| Jobs, subscriptions, podcasts and dashboard per account (nobody sees or changes another's) | `tests/unit/test_ownership_api.py` |
| scrypt, the limiter, sessions, followers, first admin, settings | `tests/unit/test_accounts_store.py` |
| `set_admin.py` | `tests/unit/test_set_admin.py` |
| The page's scripts (sign-in, set-up message, role-dependent requests, account menu, accounts page) in a fake DOM | `tests/unit/test_page_scripts.py` |
| The installer's admin step and the update path | `tests/unit/test_installer_flow.py` |
| `check-host.sh --only login` | `tests/unit/test_check_host.py` |
| A real browser: visitor sees only the form, wrong password, brake, cookie flags, logout kills a copied cookie, admin vs user tabs, creating a user, password change, cross-site POST | `tests/system/test_c43_browser.py` (gated, run on the VM) |

Tests that are not about the login run as a built-in test admin (`auth.TEST_ACCOUNT`, set only by
`tests/conftest.py`; a test checks that nothing in `app/` sets it). Tests of the real login carry
the marker `real_auth`.
