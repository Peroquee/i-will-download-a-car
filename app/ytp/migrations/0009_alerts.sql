-- The alert log and the VPN history of the dashboard (#38).
--
-- `alerts`: things a person should know about (VPN down, a failed download, a broken feed, a full
-- disk, later a failed automatic update). `title` and `detail` are English fallback texts; the
-- page translates by `kind` and fills in `params` (names, reasons, numbers; never an address or a
-- secret, the writer redacts them). `dedupe_key` makes recording the same event twice a no-op.
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    params          TEXT NOT NULL DEFAULT '{}',
    dedupe_key      TEXT UNIQUE,
    acknowledged    INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    acknowledged_at TEXT
);

-- `vpn_events`: every change of the VPN condition the app noticed through the watchdog's status
-- (the watchdog has no database; the app records the transitions).
CREATE TABLE vpn_events (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    condition  TEXT NOT NULL,
    paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    reason     TEXT NOT NULL DEFAULT ''
);
