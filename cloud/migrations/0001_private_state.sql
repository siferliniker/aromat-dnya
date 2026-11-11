-- Only revision and write time are readable; all owner state is AES-GCM encrypted.
CREATE TABLE IF NOT EXISTS private_state (
    id TEXT PRIMARY KEY NOT NULL CHECK (id = 'owner'),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
--> statement-breakpoint

-- Provider results are encrypted as well, with an AAD bound to each cache key.
CREATE TABLE IF NOT EXISTS performance_cache (
    key TEXT PRIMARY KEY NOT NULL CHECK (length(key) = 64),
    ciphertext TEXT NOT NULL,
    expires INTEGER NOT NULL
);
--> statement-breakpoint

-- A shared limit prevents separate Worker isolates from each spending 20 searches.
CREATE TABLE IF NOT EXISTS performance_guard (
    id TEXT PRIMARY KEY NOT NULL CHECK (id = 'fragella'),
    day TEXT NOT NULL,
    searches INTEGER NOT NULL DEFAULT 0 CHECK (searches BETWEEN 0 AND 20),
    blocked_until INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT ''
);
