CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE accounts (
 account_id TEXT PRIMARY KEY, platform_identity TEXT, status TEXT NOT NULL,
 pause_reason TEXT, last_success REAL, created_at REAL NOT NULL
);
CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY, profile TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
 platform_job_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE rubrics (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
 jd_version TEXT NOT NULL, rubric_version TEXT NOT NULL, scorer_profile_version TEXT NOT NULL,
 profile TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(job_id,jd_version,rubric_version,scorer_profile_version)
);
CREATE TABLE candidates (
 candidate_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
 platform_id TEXT, identity_verified INTEGER NOT NULL, label TEXT NOT NULL,
 synthetic INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 UNIQUE(account_id,platform_id)
);
CREATE TABLE candidate_jobs (
 candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES jobs(job_id), source_channel TEXT NOT NULL,
 locator TEXT NOT NULL DEFAULT '{}', human_status TEXT NOT NULL DEFAULT '未复核',
 discovered_at TEXT NOT NULL, PRIMARY KEY(candidate_id,job_id)
);
CREATE TABLE resume_snapshots (
 snapshot_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 snapshot_sha256 TEXT NOT NULL, snapshot TEXT NOT NULL, raw_text TEXT,
 created_at REAL NOT NULL, UNIQUE(candidate_id,snapshot_sha256)
);
CREATE TABLE action_targets (
 action_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES jobs(job_id), account_id TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, locator TEXT NOT NULL, verified_at REAL,
 expires_at REAL, platform_url TEXT, UNIQUE(candidate_id,job_id,kind)
);
CREATE TABLE imports (
 import_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE evaluation_batches (
 batch_id TEXT PRIMARY KEY, parent_batch_id TEXT, job_id TEXT NOT NULL,
 profile TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE batch_members (
 batch_id TEXT NOT NULL REFERENCES evaluation_batches(batch_id) ON DELETE CASCADE,
 candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'PENDING', error TEXT, run_id TEXT,
 PRIMARY KEY(batch_id,candidate_id)
);
CREATE TABLE pending_evaluations (
 eval_key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, profile TEXT NOT NULL,
 mode TEXT NOT NULL, queued_at REAL NOT NULL, batch_id TEXT
);
CREATE TABLE tasks (
 task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL,
 idempotency_key TEXT UNIQUE NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_token TEXT, lease_until REAL,
 fencing_token INTEGER NOT NULL DEFAULT 0, heartbeat REAL,
 next_run_at REAL NOT NULL, error TEXT, created_at REAL NOT NULL
);
CREATE INDEX task_ready ON tasks(kind,state,next_run_at);
CREATE TABLE evaluation_runs (
 run_id TEXT PRIMARY KEY, eval_key TEXT NOT NULL, batch_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 snapshot_id TEXT NOT NULL, harness TEXT NOT NULL, metadata TEXT NOT NULL,
 evaluation TEXT NOT NULL, score TEXT NOT NULL, operator TEXT,
 previous_run_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE current_scores (
 eval_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE outbox (
 item_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL, action_id TEXT NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
 state TEXT NOT NULL, approved_by TEXT, approved_until REAL, error TEXT,
 platform_message_id TEXT, sent_at REAL, created_at REAL NOT NULL
);
CREATE TABLE audit_events (
 id INTEGER PRIMARY KEY, object_id TEXT NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE daily_budgets (
 day TEXT NOT NULL, scope TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,scope)
);
CREATE TABLE collection_cursors (
 account_id TEXT NOT NULL, job_id TEXT NOT NULL, channel TEXT NOT NULL,
 page INTEGER NOT NULL DEFAULT 1, last_poll REAL, next_poll REAL,
 PRIMARY KEY(account_id,job_id,channel)
);
