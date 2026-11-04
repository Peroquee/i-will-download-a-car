-- Failed jobs an account has dismissed ("Ausblenden"). Additive: nothing existing changes.
--
-- Dismissing is not deleting: the job stays in `jobs` and can still be retried. The dismissal is
-- per account (the input page's "failed: N" line and the dashboard list are per viewer). The
-- trigger drops the dismissals as soon as a job leaves `failed` (a retry), so a job that fails
-- again is shown again instead of staying hidden.
CREATE TABLE job_dismissals (
    job_id       INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (job_id, account_id)
);
CREATE INDEX job_dismissals_account ON job_dismissals (account_id);

CREATE TRIGGER job_dismissals_cleared AFTER UPDATE OF status ON jobs
WHEN NEW.status <> 'failed'
BEGIN
    DELETE FROM job_dismissals WHERE job_id = NEW.id;
END;
