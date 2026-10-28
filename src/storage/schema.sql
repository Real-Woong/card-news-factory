-- Project_ContentOps SQLite schema.
-- Applied by src/storage/db.py (idempotent: safe to run on every start).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Collection sources, mirrored from config/sources.yaml so historical rows keep
-- meaning even after the YAML changes.
CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL,
    category        TEXT NOT NULL,
    trust_tier      TEXT NOT NULL,
    trust_score     INTEGER NOT NULL DEFAULT 1,
    endpoint        TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_collected_at TEXT,
    last_status     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per unique article (deduplicated by url_hash).
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash        TEXT NOT NULL UNIQUE,
    url             TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    domain          TEXT,
    title           TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    summary         TEXT,
    content         TEXT,
    content_hash    TEXT NOT NULL,
    author          TEXT,
    language        TEXT,
    category        TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_name     TEXT,
    source_type     TEXT,
    trust_tier      TEXT,
    trust_score     INTEGER NOT NULL DEFAULT 1,
    tags            TEXT,          -- JSON array
    topics          TEXT,          -- JSON array
    signals         TEXT,          -- JSON array
    published_at    TEXT,
    collected_at    TEXT NOT NULL,
    raw_path        TEXT,
    processed_path  TEXT,
    source_metadata TEXT,          -- JSON object: internal structured evidence
    -- lifecycle: new -> indexed -> selected -> used  (or duplicate)
    status          TEXT NOT NULL DEFAULT 'new',
    is_duplicate    INTEGER NOT NULL DEFAULT 0,
    dedup_reason    TEXT,
    dedup_score     REAL,
    duplicate_of    TEXT,          -- url_hash of the article this duplicates
    indexed_at      TEXT,
    score           REAL,
    score_breakdown TEXT,          -- JSON object
    selected_run_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_canonical   ON articles(canonical_url);
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_published   ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_collected   ON articles(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_category    ON articles(category, status);
CREATE INDEX IF NOT EXISTS idx_articles_status      ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_title_norm  ON articles(title_normalized);

-- Pipeline executions.
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    mode            TEXT NOT NULL DEFAULT 'live',
    stages_completed TEXT,         -- JSON array
    counts          TEXT,          -- JSON object
    output_dir      TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

-- Generated drafts / posts. Phase 2 flips status to published.
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    format          TEXT NOT NULL,
    title           TEXT,
    body            TEXT NOT NULL,
    -- draft -> approved -> published (or rejected)
    status          TEXT NOT NULL DEFAULT 'draft',
    article_keys    TEXT,          -- JSON array of url_hash values
    char_count      INTEGER,
    external_id     TEXT,
    external_url    TEXT,
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_posts_run     ON posts(run_id);
CREATE INDEX IF NOT EXISTS idx_posts_status  ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);

-- Many-to-many: which articles fed which post.
CREATE TABLE IF NOT EXISTS post_articles (
    post_id     INTEGER NOT NULL,
    url_hash    TEXT NOT NULL,
    PRIMARY KEY (post_id, url_hash),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

-- Audit trail of dedup decisions (why an item was dropped).
CREATE TABLE IF NOT EXISTS dedup_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    url_hash    TEXT NOT NULL,
    matched_key TEXT,
    reason      TEXT NOT NULL,
    score       REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dedup_url_hash ON dedup_log(url_hash);

-- Research / fact-check artefacts, kept for traceability and future RAG use.
CREATE TABLE IF NOT EXISTS research_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    url_hash    TEXT NOT NULL,
    payload     TEXT NOT NULL,     -- JSON (ResearchNote)
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fact_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    url_hash    TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    confidence  REAL,
    payload     TEXT NOT NULL,     -- JSON (FactCheckReport)
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_url  ON research_notes(url_hash);
CREATE INDEX IF NOT EXISTS idx_factcheck_url ON fact_checks(url_hash);

-- Which chunks are currently in the vector store (so re-indexing is cheap).
CREATE TABLE IF NOT EXISTS vector_index (
    chunk_id    TEXT PRIMARY KEY,
    collection  TEXT NOT NULL,
    doc_key     TEXT NOT NULL,     -- url_hash, post id, or knowledge file path
    doc_type    TEXT NOT NULL,     -- article | post | knowledge
    content_hash TEXT,
    indexed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vector_doc ON vector_index(doc_key, collection);

-- Convenience view: everything eligible for ranking.
CREATE VIEW IF NOT EXISTS v_active_articles AS
SELECT *
FROM articles
WHERE is_duplicate = 0
  AND status != 'rejected';
