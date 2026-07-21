CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    queue_seq INTEGER UNIQUE,
    idempotency_key_hash TEXT,
    request_fingerprint TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    business_ref TEXT,
    source_extension TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_bytes INTEGER NOT NULL CHECK (source_bytes >= 0),
    input_path TEXT NOT NULL,
    job_state TEXT NOT NULL CHECK (job_state IN (
        'queued', 'waiting_for_result', 'running', 'publishing', 'succeeded',
        'failed', 'cancelled', 'expired', 'result_expired'
    )),
    cache_role TEXT NOT NULL CHECK (cache_role IN ('owner', 'follower', 'hit')),
    result_source TEXT CHECK (result_source IN ('ocr', 'shared_inflight', 'cache')),
    callback_url TEXT,
    processing_attempt INTEGER NOT NULL DEFAULT 0 CHECK (processing_attempt >= 0),
    active_attempt_token TEXT,
    worker_id TEXT,
    lease_expires_at INTEGER,
    result_path TEXT,
    result_bytes INTEGER CHECK (result_bytes >= 0),
    source_page_count INTEGER CHECK (source_page_count >= 0),
    processed_page_count INTEGER CHECK (processed_page_count >= 0),
    truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    warnings_json TEXT,
    result_expires_at INTEGER,
    tombstone_expires_at INTEGER,
    submitted_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    error_code TEXT,
    error_message TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_tenant_idempotency_key_unique
    ON jobs(tenant_id, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_state_queue_seq_index ON jobs(job_state, queue_seq);
CREATE INDEX IF NOT EXISTS jobs_tenant_job_index ON jobs(tenant_id, job_id);
CREATE INDEX IF NOT EXISTS jobs_result_expires_at_index ON jobs(result_expires_at);
CREATE INDEX IF NOT EXISTS jobs_lease_expires_at_index ON jobs(lease_expires_at);

CREATE TABLE IF NOT EXISTS job_queue_sequence (
    sequence_name TEXT PRIMARY KEY,
    last_value INTEGER NOT NULL
);
INSERT OR IGNORE INTO job_queue_sequence(sequence_name, last_value) VALUES ('ocr', 0);

CREATE TABLE IF NOT EXISTS parse_cache (
    tenant_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    cache_state TEXT NOT NULL CHECK (cache_state IN ('processing', 'ready')),
    owner_job_id TEXT,
    result_path TEXT,
    result_bytes INTEGER CHECK (result_bytes >= 0),
    created_at INTEGER NOT NULL,
    last_accessed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, source_sha256),
    FOREIGN KEY (owner_job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS parse_cache_state_expires_at_index ON parse_cache(cache_state, expires_at);

CREATE TABLE IF NOT EXISTS callback_outbox (
    delivery_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    callback_url_snapshot TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    callback_state TEXT NOT NULL CHECK (callback_state IN (
        'pending', 'dispatching', 'delivered', 'failed'
    )),
    attempted_at INTEGER,
    http_status INTEGER,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_heartbeats (
    component_type TEXT NOT NULL,
    component_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    component_state TEXT NOT NULL,
    last_seen_at INTEGER NOT NULL,
    details_json TEXT,
    PRIMARY KEY (component_type, component_id)
);
