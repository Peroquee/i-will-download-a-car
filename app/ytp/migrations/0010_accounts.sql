-- Own accounts for the app, and who owns what (#44). Additive: nothing existing changes.
--
-- `accounts`: name + scrypt password hash + role. `sessions`: the login cookie, stored only as a
-- SHA-256 of its value (a copy of the database cannot be turned into a logged-in browser).
-- `job_owners`, `subscription_followers`, `podcast_followers`: many-to-many, because one video
-- or one channel is downloaded once however many people asked for it: the second person to ask
-- becomes another owner / follower of the same row. Rows that exist before the first account
-- are given to the first admin when that account is created (accounts.adopt_orphans).
CREATE TABLE accounts (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(username) BETWEEN 3 AND 64),
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login    TEXT
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX sessions_account ON sessions (account_id);

CREATE TABLE job_owners (
    job_id     INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, account_id)
);
CREATE INDEX job_owners_account ON job_owners (account_id);

CREATE TABLE subscription_followers (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    account_id      INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, account_id)
);
CREATE INDEX subscription_followers_account ON subscription_followers (account_id);

CREATE TABLE podcast_followers (
    subscription_id INTEGER NOT NULL REFERENCES podcast_subscriptions (id) ON DELETE CASCADE,
    account_id      INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, account_id)
);
CREATE INDEX podcast_followers_account ON podcast_followers (account_id);
