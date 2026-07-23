CREATE TABLE IF NOT EXISTS jobs (
    job_id VARCHAR(26) PRIMARY KEY,
    tenant_id VARCHAR(255) NOT NULL,
    queue_seq BIGINT UNIQUE,
    idempotency_key_hash CHAR(64),
    request_fingerprint CHAR(64) NOT NULL,
    source_filename VARCHAR(1024) NOT NULL,
    stored_filename VARCHAR(1024) NOT NULL,
    business_ref VARCHAR(255),
    source_sha256 CHAR(64) NOT NULL,
    source_bytes BIGINT NOT NULL,
    job_state VARCHAR(32) NOT NULL,
    cache_role VARCHAR(16) NOT NULL,
    callback_url TEXT,
    processing_attempt INT NOT NULL DEFAULT 0,
    active_attempt_token VARCHAR(64),
    lease_expires_at BIGINT,
    result_path VARCHAR(2048),
    source_page_count INT,
    processed_page_count INT,
    truncated TINYINT(1) NOT NULL DEFAULT 0,
    warnings_json JSON,
    result_expires_at BIGINT,
    tombstone_expires_at BIGINT,
    submitted_at BIGINT NOT NULL,
    started_at BIGINT,
    finished_at BIGINT,
    error_code VARCHAR(128),
    error_message TEXT,
    UNIQUE KEY jobs_tenant_idempotency_key_unique (tenant_id, idempotency_key_hash),
    KEY jobs_state_queue_seq_index (job_state, queue_seq),
    KEY jobs_tenant_job_index (tenant_id, job_id),
    KEY jobs_result_expires_at_index (result_expires_at),
    KEY jobs_lease_expires_at_index (lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS job_queue_sequence (
    sequence_name VARCHAR(32) PRIMARY KEY,
    sequence_value BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO job_queue_sequence(sequence_name, sequence_value) VALUES ('ocr', 0);

CREATE TABLE IF NOT EXISTS parse_cache (
    tenant_id VARCHAR(255) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    cache_state VARCHAR(16) NOT NULL,
    owner_job_id VARCHAR(26),
    result_path VARCHAR(2048),
    result_bytes BIGINT,
    created_at BIGINT NOT NULL,
    last_accessed_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, source_sha256),
    CONSTRAINT parse_cache_owner_fk FOREIGN KEY (owner_job_id) REFERENCES jobs(job_id),
    KEY parse_cache_state_expires_at_index (cache_state, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS callback_outbox (
    delivery_id VARCHAR(26) PRIMARY KEY,
    job_id VARCHAR(26) NOT NULL UNIQUE,
    callback_url_snapshot TEXT NOT NULL,
    payload_json JSON NOT NULL,
    callback_state VARCHAR(16) NOT NULL,
    attempted_at BIGINT,
    http_status INT,
    error_code VARCHAR(128),
    error_message TEXT,
    CONSTRAINT callback_outbox_job_fk FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_heartbeats (
    component_type VARCHAR(64) NOT NULL,
    component_id VARCHAR(128) NOT NULL,
    pid INT NOT NULL,
    component_state VARCHAR(32) NOT NULL,
    last_seen_at BIGINT NOT NULL,
    details_json JSON,
    PRIMARY KEY (component_type, component_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
