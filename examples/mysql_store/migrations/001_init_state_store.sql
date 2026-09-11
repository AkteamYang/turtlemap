CREATE TABLE IF NOT EXISTS session_state (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id VARCHAR(128) NOT NULL,
    schema_version INT  DEFAULT 0,
    version INT DEFAULT 0,
    state_json JSON NOT NULL,
    save_kind VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_session_state (session_id, schema_version, version),
    KEY idx_session_state_latest (session_id, schema_version, version, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_long_term_memory (
    uid BIGINT NOT NULL,
    memory_text MEDIUMTEXT NOT NULL,
    version BIGINT PRIMARY KEY AUTO_INCREMENT,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_agent_long_term_memory_latest (uid, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS context_compression_task (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id VARCHAR(128) NOT NULL,
    agent_name VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    running_task_key VARCHAR(512)
        GENERATED ALWAYS AS (
            CASE
                WHEN status = 'running' THEN CONCAT(session_id, '-', agent_name)
                ELSE NULL
            END
        ) STORED,
    lease_owner VARCHAR(128) NULL,
    lease_expires_at DATETIME NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_context_compression_task (running_task_key),
    KEY idx_context_compression_running (
        session_id,
        agent_name,
        status,
        lease_expires_at
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
