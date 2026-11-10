-- Per-subscription check interval and check time (issue #79).
--
-- `poll_minutes`: how often the feed is looked at, in minutes; 0 = paused (never checked by the
-- schedule), otherwise 15..1440 for a channel and 30..1440 for a podcast. NULL = the server-wide
-- default (the admin's value, else SUBSCRIPTION_POLL_MINUTES / PODCAST_POLL_MINUTES).
-- `poll_at`: an optional start time of day "HH:MM" (server time zone) that anchors the interval.
-- NULL = the server-wide default; '' = explicitly none, even if the server has one.
ALTER TABLE subscriptions ADD COLUMN poll_minutes INTEGER NULL
    CHECK (poll_minutes IS NULL OR poll_minutes = 0 OR poll_minutes BETWEEN 15 AND 1440);
ALTER TABLE subscriptions ADD COLUMN poll_at TEXT NULL
    CHECK (poll_at IS NULL OR poll_at = ''
           OR (poll_at GLOB '[0-2][0-9]:[0-5][0-9]'
               AND CAST(substr(poll_at, 1, 2) AS INTEGER) <= 23));

ALTER TABLE podcast_subscriptions ADD COLUMN poll_minutes INTEGER NULL
    CHECK (poll_minutes IS NULL OR poll_minutes = 0 OR poll_minutes BETWEEN 30 AND 1440);
ALTER TABLE podcast_subscriptions ADD COLUMN poll_at TEXT NULL
    CHECK (poll_at IS NULL OR poll_at = ''
           OR (poll_at GLOB '[0-2][0-9]:[0-5][0-9]'
               AND CAST(substr(poll_at, 1, 2) AS INTEGER) <= 23));
