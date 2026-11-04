-- Videos of a subscribed channel that the poller has looked at and settled (queued, skipped, or
-- older than the subscription), so the next poll can stop at the first one it knows. Needed since
-- the channel is listed with yt-dlp (no dates in the list) instead of the RSS feed (with dates).
-- The rows go with their subscription; the poller keeps only the newest few hundred.
CREATE TABLE subscription_seen_videos (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions (id) ON DELETE CASCADE,
    video_id        TEXT NOT NULL,
    UNIQUE (subscription_id, video_id)
);
