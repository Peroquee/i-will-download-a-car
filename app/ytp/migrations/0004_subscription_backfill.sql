-- How many of a channel's existing videos the FIRST poll should also fetch (#19, used by #20).
-- 0 (the default) = only videos published after subscribing, never the back catalogue. The
-- channel's RSS feed lists at most 15 videos, so that is the ceiling.
ALTER TABLE subscriptions ADD COLUMN backfill INTEGER NOT NULL DEFAULT 0
    CHECK (backfill BETWEEN 0 AND 15);
