-- A small key/value table for things the app itself needs to remember between runs (#12):
-- when the scheduler last made progress, and later whatever a job wants to keep (a cursor,
-- a last-seen marker). Not for domain data: jobs and subscriptions have their own tables.
CREATE TABLE app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (key <> '')
) WITHOUT ROWID;
