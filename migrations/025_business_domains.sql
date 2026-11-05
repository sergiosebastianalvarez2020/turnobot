BEGIN IMMEDIATE;

CREATE TABLE business_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    hostname TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (verification_status IN ('pending', 'verified', 'revoked')),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    verification_token_hash TEXT,
    verification_expires_at TEXT,
    verified_at TEXT,
    tls_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (tls_status IN ('pending', 'verified', 'failed')),
    tls_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (verification_status = 'pending'
            AND verification_token_hash IS NOT NULL
            AND verification_expires_at IS NOT NULL)
        OR
        (verification_status <> 'pending'
            AND verification_token_hash IS NULL
            AND verification_expires_at IS NULL)
    ),
    CHECK (is_active = 0 OR (
        verification_status = 'verified' AND tls_status = 'verified'
    )),
    CHECK (is_primary = 0 OR (is_active = 1 AND tls_status = 'verified')),
    CHECK (tls_status = 'pending' OR verification_status = 'verified')
);

CREATE UNIQUE INDEX uq_business_domains_hostname
    ON business_domains (lower(hostname));
CREATE INDEX idx_business_domains_business
    ON business_domains (business_id);
CREATE UNIQUE INDEX uq_business_domains_primary
    ON business_domains (business_id)
    WHERE is_primary = 1;

COMMIT;
