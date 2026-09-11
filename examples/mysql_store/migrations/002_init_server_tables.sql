CREATE TABLE IF NOT EXISTS session_meta (
    session_id VARCHAR(128) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    deleted TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_session_meta_list (deleted, updated_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS message_projection (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    task_id VARCHAR(128) NULL,
    client_event_id VARCHAR(128) NULL,
    content_json JSON NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_message_projection_session_id (session_id, id),
    KEY idx_message_projection_client_event (session_id, client_event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS message_projection_run (
    run_id VARCHAR(128) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_message_projection_run_session (session_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
