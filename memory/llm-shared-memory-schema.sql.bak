PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    scope TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES events(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_unique
ON observations(source_event_id, entity_type, entity_id, attribute, extractor_version);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    salience REAL NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    project_id TEXT,
    status TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active
ON memories(entity_type, entity_id, key, scope, COALESCE(project_id, ''))
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY(memory_id, observation_id),
    FOREIGN KEY(memory_id) REFERENCES memories(id),
    FOREIGN KEY(observation_id) REFERENCES observations(id)
);

CREATE TABLE IF NOT EXISTS retrieval_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    query TEXT NOT NULL,
    returned_memory_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS deletions (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
