CREATE TABLE IF NOT EXISTS accounts(
    name TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    verifier TEXT,
    changed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
    digest TEXT PRIMARY KEY,
    account TEXT NOT NULL REFERENCES accounts(name),
    expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account);
CREATE TABLE IF NOT EXISTS login_limits(
    bucket TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    since INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS used_totp(
    account TEXT PRIMARY KEY REFERENCES accounts(name),
    counter INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_log(
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    target TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
