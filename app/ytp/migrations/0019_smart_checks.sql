-- Smart check times (issue #80): learn when a channel or podcast usually publishes.
--
-- `publish_history`: the publication times of a subscription's items (YouTube videos, podcast
-- episodes), unique per item, newest ~100 kept per subscription (the code trims). `kind` is
-- 'channel' or 'podcast' and `subscription_id` the id in `subscriptions` / `podcast_subscriptions`;
-- there is no foreign key because the id belongs to one of two tables (a deleted subscription's
-- rows are removed by the code).
CREATE TABLE publish_history (
    kind            TEXT    NOT NULL CHECK (kind IN ('channel', 'podcast')),
    subscription_id INTEGER NOT NULL,
    item_id         TEXT    NOT NULL,
    published_at    TEXT    NOT NULL,
    PRIMARY KEY (kind, subscription_id, item_id)
) WITHOUT ROWID;
CREATE INDEX publish_history_by_time ON publish_history (kind, subscription_id, published_at);

-- `poll_mode`: 'smart' (check around the learned publishing times, falling back to the interval
-- while the pattern is unknown) or 'fixed' (the #79 interval and start time). NULL = the
-- server-wide default, which is 'smart' until an admin changes it: so every existing subscription
-- becomes smart without a row being rewritten.
-- `poll_errors`: failed checks in a row (the retry waits longer each time).
-- `history_checked_at`: when the publication history was last filled from the channel's RSS feed
-- (the yt-dlp list has no times), so that is asked rarely.
ALTER TABLE subscriptions ADD COLUMN poll_mode TEXT NULL
    CHECK (poll_mode IS NULL OR poll_mode IN ('smart', 'fixed'));
ALTER TABLE subscriptions ADD COLUMN poll_errors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE subscriptions ADD COLUMN history_checked_at TEXT NULL;

ALTER TABLE podcast_subscriptions ADD COLUMN poll_mode TEXT NULL
    CHECK (poll_mode IS NULL OR poll_mode IN ('smart', 'fixed'));
ALTER TABLE podcast_subscriptions ADD COLUMN poll_errors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE podcast_subscriptions ADD COLUMN history_checked_at TEXT NULL;
