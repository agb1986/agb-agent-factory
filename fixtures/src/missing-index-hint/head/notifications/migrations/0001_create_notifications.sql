-- The notifications table. Only the primary key is indexed.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    read_at TEXT
);
