-- Videos of a subscribed channel that the poller deliberately did not queue (#39): live streams,
-- premieres and Shorts. Remembered so a video is judged once (no second probe on every poll) and
-- shown in the subscription's details ("skipped: Short"). Not a job and not a failure. The rows
-- go with their subscription.
CREATE TABLE skipped_videos (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    video_id        TEXT NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('live', 'premiere', 'short')),
    title           TEXT NOT NULL DEFAULT '',
    published       TEXT,
    skipped_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (subscription_id, video_id)
);
