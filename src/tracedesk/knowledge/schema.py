"""Additive schema 2: immutable sources and transactionally published projections."""

KNOWLEDGE_SCHEMA = (
    """CREATE TABLE knowledge_documents (
        id TEXT PRIMARY KEY NOT NULL, tenant_id TEXT NOT NULL,
        title TEXT NOT NULL CHECK (length(title) > 0), created_at TEXT NOT NULL,
        UNIQUE (id, tenant_id), UNIQUE (tenant_id, title)
    )""",
    """CREATE TABLE knowledge_versions (
        document_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version > 0),
        filename TEXT NOT NULL, media_type TEXT NOT NULL,
        source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
        original BLOB NOT NULL CHECK (typeof(original) = 'blob' AND length(original) > 0),
        parser TEXT NOT NULL, parse_id TEXT NOT NULL UNIQUE, parse_started_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('parsing','ready','failed')),
        created_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
        finished_at TEXT, error_json TEXT,
        PRIMARY KEY (document_id, tenant_id, version),
        UNIQUE (tenant_id, source_sha256),
        FOREIGN KEY (document_id, tenant_id) REFERENCES knowledge_documents(id, tenant_id),
        CHECK (
            (status = 'parsing' AND finished_at IS NULL AND error_json IS NULL) OR
            (status = 'ready' AND finished_at IS NOT NULL AND error_json IS NULL) OR
            (status = 'failed' AND finished_at IS NOT NULL AND error_json IS NOT NULL)
        )
    )""",
    """CREATE TABLE knowledge_parse_attempts (
        id TEXT PRIMARY KEY NOT NULL, document_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        version INTEGER NOT NULL, parser TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('ready','failed')),
        error_json TEXT,
        FOREIGN KEY (document_id, tenant_id, version)
            REFERENCES knowledge_versions(document_id, tenant_id, version),
        CHECK ((status = 'ready' AND error_json IS NULL) OR
               (status = 'failed' AND error_json IS NOT NULL))
    )""",
    """CREATE TABLE knowledge_blocks (
        source_id TEXT PRIMARY KEY NOT NULL, document_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL, version INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal >= 0),
        text TEXT NOT NULL CHECK (length(text) > 0),
        page INTEGER CHECK (page IS NULL OR (typeof(page) = 'integer' AND page > 0)),
        kind TEXT NOT NULL CHECK (kind IN ('text','table')),
        block_sha256 TEXT NOT NULL CHECK (length(block_sha256) = 64),
        UNIQUE (source_id, tenant_id), UNIQUE (document_id, tenant_id, version, ordinal),
        FOREIGN KEY (document_id, tenant_id, version)
            REFERENCES knowledge_versions(document_id, tenant_id, version)
    )""",
    """CREATE TABLE knowledge_snapshots (
        id TEXT PRIMARY KEY NOT NULL, tenant_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('building','published','failed')),
        created_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL, finished_at TEXT,
        with_embeddings INTEGER NOT NULL CHECK (with_embeddings IN (0,1)),
        embedding_fingerprint TEXT,
        source_count INTEGER NOT NULL CHECK (typeof(source_count) = 'integer' AND source_count > 0),
        artifacts_json TEXT NOT NULL DEFAULT '{}', error_json TEXT,
        UNIQUE (id, tenant_id),
        CHECK (
            (with_embeddings = 0 AND embedding_fingerprint IS NULL) OR
            (with_embeddings = 1 AND embedding_fingerprint IS NOT NULL)
        ),
        CHECK (
            (status = 'building' AND finished_at IS NULL AND error_json IS NULL) OR
            (status = 'published' AND finished_at IS NOT NULL AND error_json IS NULL) OR
            (status = 'failed' AND finished_at IS NOT NULL AND error_json IS NOT NULL)
        )
    )""",
    """CREATE UNIQUE INDEX one_knowledge_build_per_tenant
        ON knowledge_snapshots(tenant_id) WHERE status = 'building'""",
    """CREATE TABLE knowledge_snapshot_members (
        snapshot_id TEXT NOT NULL, tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, tenant_id, source_id),
        FOREIGN KEY (snapshot_id, tenant_id) REFERENCES knowledge_snapshots(id, tenant_id),
        FOREIGN KEY (source_id, tenant_id) REFERENCES knowledge_blocks(source_id, tenant_id)
    )""",
    """CREATE TABLE knowledge_vectors (
        snapshot_id TEXT NOT NULL, tenant_id TEXT NOT NULL, source_id TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK (typeof(dimensions) = 'integer' AND dimensions > 0),
        vector_json TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, tenant_id, source_id),
        FOREIGN KEY (snapshot_id, tenant_id, source_id)
            REFERENCES knowledge_snapshot_members(snapshot_id, tenant_id, source_id)
    )""",
    """CREATE TABLE knowledge_publications (
        tenant_id TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL,
        FOREIGN KEY (snapshot_id, tenant_id) REFERENCES knowledge_snapshots(id, tenant_id)
    )""",
    """CREATE TRIGGER knowledge_version_transition BEFORE UPDATE ON knowledge_versions
        WHEN NEW.document_id != OLD.document_id OR NEW.tenant_id != OLD.tenant_id
        OR NEW.version != OLD.version OR NEW.filename != OLD.filename
        OR NEW.media_type != OLD.media_type OR NEW.source_sha256 != OLD.source_sha256
        OR NEW.original != OLD.original OR NEW.created_at != OLD.created_at
        OR NOT (
            (OLD.status = 'parsing' AND NEW.status IN ('ready','failed')
             AND NEW.parse_id = OLD.parse_id AND NEW.parser = OLD.parser
             AND NEW.parse_started_at = OLD.parse_started_at
             AND NEW.lease_expires_at = OLD.lease_expires_at)
            OR (OLD.status = 'failed' AND NEW.status = 'parsing' AND NEW.parse_id != OLD.parse_id)
        )
        BEGIN SELECT RAISE(ABORT, 'document source is immutable'); END""",
    """CREATE TRIGGER knowledge_log_parse_result AFTER UPDATE OF status ON knowledge_versions
        WHEN NEW.status IN ('ready','failed')
        BEGIN
            INSERT INTO knowledge_parse_attempts(
                id, document_id, tenant_id, version, parser, started_at, finished_at, status, error_json
            ) VALUES (
                NEW.parse_id, NEW.document_id, NEW.tenant_id, NEW.version, NEW.parser,
                NEW.parse_started_at, NEW.finished_at, NEW.status, NEW.error_json
            );
        END""",
    """CREATE TRIGGER knowledge_blocks_require_parsing BEFORE INSERT ON knowledge_blocks
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_versions WHERE document_id = NEW.document_id
            AND tenant_id = NEW.tenant_id AND version = NEW.version AND status = 'parsing'
        )
        BEGIN SELECT RAISE(ABORT, 'blocks require a parsing document'); END""",
    """CREATE TRIGGER knowledge_ready_requires_blocks BEFORE UPDATE ON knowledge_versions
        WHEN NEW.status = 'ready' AND NOT EXISTS (
            SELECT 1 FROM knowledge_blocks WHERE document_id = NEW.document_id
            AND tenant_id = NEW.tenant_id AND version = NEW.version
        )
        BEGIN SELECT RAISE(ABORT, 'ready documents require source blocks'); END""",
    """CREATE TRIGGER knowledge_snapshot_transition BEFORE UPDATE ON knowledge_snapshots
        WHEN OLD.status != 'building' OR NEW.status = 'building'
        OR NEW.id != OLD.id OR NEW.tenant_id != OLD.tenant_id
        OR NEW.created_at != OLD.created_at OR NEW.lease_expires_at != OLD.lease_expires_at
        OR NEW.with_embeddings != OLD.with_embeddings
        OR NEW.embedding_fingerprint IS NOT OLD.embedding_fingerprint
        OR NEW.source_count != OLD.source_count
        BEGIN SELECT RAISE(ABORT, 'snapshot is immutable'); END""",
    """CREATE TRIGGER knowledge_publish_requires_complete_projection BEFORE UPDATE ON knowledge_snapshots
        WHEN NEW.status = 'published' AND (
            NEW.source_count != (
                SELECT count(*) FROM knowledge_snapshot_members
                WHERE snapshot_id = NEW.id AND tenant_id = NEW.tenant_id
            ) OR (NEW.with_embeddings = 1 AND NEW.source_count != (
                SELECT count(*) FROM knowledge_vectors
                WHERE snapshot_id = NEW.id AND tenant_id = NEW.tenant_id
            ))
        )
        BEGIN SELECT RAISE(ABORT, 'published snapshot projection is incomplete'); END""",
    *(
        f"""CREATE TRIGGER {table}_require_building BEFORE INSERT ON {table}
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_snapshots WHERE id = NEW.snapshot_id
                AND tenant_id = NEW.tenant_id AND status = 'building'
            )
            BEGIN SELECT RAISE(ABORT, 'projection requires a building snapshot'); END"""
        for table in ("knowledge_snapshot_members", "knowledge_vectors")
    ),
    *(
        f"""CREATE TRIGGER knowledge_publication_{operation.lower()}
            BEFORE {operation} ON knowledge_publications
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_snapshots WHERE id = NEW.snapshot_id
                AND tenant_id = NEW.tenant_id AND status = 'published'
            )
            BEGIN SELECT RAISE(ABORT, 'only published snapshots may be activated'); END"""
        for operation in ("INSERT", "UPDATE")
    ),
    *(
        f"""CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT, 'knowledge source or projection is immutable'); END"""
        for table in (
            "knowledge_documents",
            "knowledge_parse_attempts",
            "knowledge_blocks",
            "knowledge_snapshot_members",
            "knowledge_vectors",
        )
        for operation in ("UPDATE", "DELETE")
    ),
    *(
        f"""CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'knowledge history is immutable'); END"""
        for table in ("knowledge_versions", "knowledge_snapshots")
    ),
)
