-- A short note on a finished job (issue #69).
--
-- The organizer can finish with `done` while some files were skipped or already existed in the
-- library with other content (a season pack with only 1 of 10 episodes linked). `error` is for
-- failures only (it is cleared for every other status), so the job gets its own `note` column
-- for that: NULL means "nothing to say", otherwise a short line for the job list.
ALTER TABLE jobs ADD COLUMN note TEXT;
