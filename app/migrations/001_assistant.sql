-- Sewa Setu assistant channel: tables and guards added alongside the portal.
-- Idempotent: safe to run at every start.

-- One active application per mobile, enforced by the database rather than by a
-- check-then-insert that two retried requests can both pass. (The seed holds no
-- violations, so this applies cleanly to the production snapshot.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_application_per_mobile
    ON applications (mobile) WHERE status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED');

-- Application numbers are random; make collisions impossible instead of unlikely.
CREATE UNIQUE INDEX IF NOT EXISTS uq_application_no ON applications (application_no);

-- Duplicate-beneficiary lookups for the officer's assistant (name + date of birth).
CREATE INDEX IF NOT EXISTS idx_app_name_dob ON applications (lower(applicant_name), dob);
CREATE INDEX IF NOT EXISTS idx_app_bank ON applications (bank_account);

-- Named block officers. The env ADMIN_USERNAME remains the district (all-blocks) login.
CREATE TABLE IF NOT EXISTS officers (
    username      VARCHAR(40) PRIMARY KEY,
    full_name     VARCHAR(100) NOT NULL,
    block         VARCHAR(50),               -- NULL = all blocks
    password_hash VARCHAR(200) NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- An application being prepared through an assistant. Lives on the server, keyed by the
-- signed-in mobile, so it survives the 2-hour sign-in limit and a change of device.
CREATE TABLE IF NOT EXISTS agent_drafts (
    mobile     VARCHAR(15) PRIMARY KEY,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    doc_path   VARCHAR(200),
    doc_name   VARCHAR(100),
    updated_at TIMESTAMP NOT NULL
);

-- Short-lived links for uploading an age proof outside the chat.
CREATE TABLE IF NOT EXISTS upload_tokens (
    token_hash VARCHAR(64) PRIMARY KEY,
    mobile     VARCHAR(15) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at    TIMESTAMP
);
