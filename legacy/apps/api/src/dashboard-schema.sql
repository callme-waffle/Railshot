CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS preferences (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  data TEXT NOT NULL CHECK(json_valid(data))
) STRICT;
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT,
  record TEXT NOT NULL CHECK(json_valid(record))
) STRICT;
CREATE INDEX IF NOT EXISTS operations_session_created ON operations(session_id, created_at DESC);
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id),
  environment_target_id TEXT NOT NULL,
  app TEXT NOT NULL,
  record TEXT NOT NULL CHECK(json_valid(record)),
  UNIQUE(environment_target_id, app)
) STRICT;
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id),
  record TEXT NOT NULL CHECK(json_valid(record))
) STRICT;
CREATE TABLE IF NOT EXISTS bindings (
  run_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  record TEXT NOT NULL CHECK(json_valid(record))
) STRICT;
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id)
) STRICT;
CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  provider TEXT NOT NULL CHECK(provider = 'openstack'),
  label TEXT NOT NULL,
  console_url TEXT NOT NULL,
  username TEXT NOT NULL,
  password_encrypted BLOB,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS connections_session ON connections(session_id);
CREATE TABLE IF NOT EXISTS owners (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
  token_hash TEXT NOT NULL UNIQUE,
  recovery_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS personal_state (
  id TEXT PRIMARY KEY,
  record TEXT NOT NULL CHECK(json_valid(record))
) STRICT;
CREATE TABLE IF NOT EXISTS registrations (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  provider TEXT NOT NULL CHECK(provider = 'openstack'),
  status TEXT NOT NULL CHECK(status IN ('pending', 'claimed')),
  created_at TEXT NOT NULL,
  claimed_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS registrations_session_created ON registrations(session_id, created_at DESC);
CREATE TABLE IF NOT EXISTS registration_tokens (
  registration_id TEXT PRIMARY KEY REFERENCES registrations(id),
  token_hash TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER,
  created_at INTEGER NOT NULL
) STRICT;
