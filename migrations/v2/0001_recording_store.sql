PRAGMA foreign_keys = ON;

BEGIN;

-- Storage v2 must live in a separate database. Abort before any persistent DDL
-- when the connection points at the legacy jobs/deliveries database.
CREATE TEMP TABLE storage_v2_preflight_guard (
    legacy_table_count INTEGER NOT NULL CHECK (legacy_table_count = 0)
);
INSERT INTO storage_v2_preflight_guard(legacy_table_count)
SELECT COUNT(*)
FROM sqlite_master
WHERE type = 'table' AND name IN ('jobs', 'deliveries');
DROP TABLE storage_v2_preflight_guard;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT
        CHECK (
            checksum_sha256 IS NULL
            OR (
                length(checksum_sha256) = 64
                AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    storage_key TEXT NOT NULL UNIQUE
        CHECK (
            storage_key <> ''
            AND storage_key NOT GLOB '*[^0-9A-Za-z_-]*'
        ),
    original_name_raw TEXT NOT NULL CHECK (original_name_raw <> ''),
    original_name_nfc TEXT NOT NULL CHECK (original_name_nfc <> ''),
    source_relpath TEXT NOT NULL
        CHECK (
            source_relpath <> ''
            AND source_relpath NOT LIKE '/%'
            AND instr(source_relpath, '\') = 0
            AND source_relpath <> '..'
            AND instr(source_relpath, '../') = 0
            AND instr(source_relpath, '/..') = 0
        ),
    manifest_relpath TEXT NOT NULL DEFAULT 'manifest.json'
        CHECK (
            manifest_relpath <> ''
            AND manifest_relpath NOT LIKE '/%'
            AND instr(manifest_relpath, '\') = 0
            AND manifest_relpath <> '..'
            AND instr(manifest_relpath, '../') = 0
            AND instr(manifest_relpath, '/..') = 0
        ),
    ingest_sha256 TEXT,
    ingest_bytes INTEGER CHECK (ingest_bytes IS NULL OR ingest_bytes >= 0),
    source_mime TEXT,
    source_state TEXT NOT NULL DEFAULT 'available'
        CHECK (source_state IN ('available', 'missing')),
    source_error_code TEXT,
    recorded_at TEXT,
    recorded_at_source TEXT
        CHECK (
            recorded_at_source IS NULL
            OR recorded_at_source IN ('audio_metadata', 'file_created_at', 'received_at', 'manual', 'legacy_import')
        ),
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS recordings_storage_key_immutable
BEFORE UPDATE OF storage_key ON recordings
FOR EACH ROW
WHEN NEW.storage_key <> OLD.storage_key
BEGIN
    SELECT RAISE(ABORT, 'recordings.storage_key is immutable');
END;

CREATE TABLE IF NOT EXISTS recording_titles (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_source TEXT NOT NULL
        CHECK (title_source IN ('manual', 'schedule', 'filename_inference', 'legacy_import', 'system')),
    locale TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, recording_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS recording_titles_one_current_per_recording
ON recording_titles(recording_id)
WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS recording_contexts (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    context_type TEXT NOT NULL
        CHECK (context_type IN ('general', 'class_session', 'daily_note', 'meeting', 'memo')),
    label TEXT,
    semester TEXT,
    course_name TEXT,
    course_code TEXT,
    session_date TEXT,
    period_label TEXT,
    period_index INTEGER CHECK (period_index IS NULL OR period_index > 0),
    context_json TEXT CHECK (context_json IS NULL OR json_valid(context_json)),
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'schedule_import', 'filename_inference', 'legacy_import', 'system')),
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK (is_selected IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, recording_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS recording_contexts_one_selected_per_recording
ON recording_contexts(recording_id)
WHERE is_selected = 1;

CREATE TABLE IF NOT EXISTS schedule_imports (
    id INTEGER PRIMARY KEY,
    semester TEXT NOT NULL CHECK (semester <> '' AND length(semester) <= 128),
    source_format TEXT NOT NULL CHECK (source_format IN ('csv', 'json')),
    source_sha256 TEXT NOT NULL
        CHECK (
            length(source_sha256) = 64
            AND source_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    entries_sha256 TEXT NOT NULL
        CHECK (
            length(entries_sha256) = 64
            AND entries_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (semester, entries_sha256),
    UNIQUE (id, semester)
);

CREATE TRIGGER IF NOT EXISTS schedule_imports_immutable
BEFORE UPDATE ON schedule_imports
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'schedule_imports rows are immutable');
END;

CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY,
    schedule_import_id INTEGER NOT NULL
        REFERENCES schedule_imports(id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL CHECK (row_index > 0),
    entry_key TEXT NOT NULL
        CHECK (
            length(entry_key) = 64
            AND entry_key NOT GLOB '*[^0-9a-f]*'
        ),
    semester TEXT NOT NULL CHECK (semester <> '' AND length(semester) <= 128),
    course_name TEXT NOT NULL CHECK (course_name <> '' AND length(course_name) <= 256),
    course_code TEXT CHECK (course_code IS NULL OR length(course_code) <= 128),
    weekday TEXT NOT NULL
        CHECK (weekday IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')),
    start_time TEXT NOT NULL
        CHECK (
            length(start_time) = 5
            AND start_time GLOB '[0-2][0-9]:[0-5][0-9]'
            AND CAST(substr(start_time, 1, 2) AS INTEGER) BETWEEN 0 AND 23
        ),
    end_time TEXT NOT NULL
        CHECK (
            length(end_time) = 5
            AND end_time GLOB '[0-2][0-9]:[0-5][0-9]'
            AND CAST(substr(end_time, 1, 2) AS INTEGER) BETWEEN 0 AND 23
        ),
    period_label TEXT NOT NULL CHECK (period_label <> '' AND length(period_label) <= 128),
    period_index INTEGER CHECK (period_index IS NULL OR period_index > 0),
    classroom TEXT NOT NULL CHECK (classroom <> '' AND length(classroom) <= 256),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (start_time < end_time),
    UNIQUE (schedule_import_id, row_index),
    UNIQUE (schedule_import_id, entry_key),
    UNIQUE (id, semester)
);

CREATE TRIGGER IF NOT EXISTS schedule_entries_immutable
BEFORE UPDATE ON schedule_entries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'schedule_entries rows are immutable');
END;

CREATE TABLE IF NOT EXISTS schedule_semester_selections (
    semester TEXT PRIMARY KEY
        CHECK (semester <> '' AND length(semester) <= 128),
    schedule_import_id INTEGER NOT NULL UNIQUE,
    selection_plan_sha256 TEXT NOT NULL
        CHECK (
            length(selection_plan_sha256) = 64
            AND selection_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (schedule_import_id, semester)
        REFERENCES schedule_imports(id, semester) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS schedule_semester_selections_semester_immutable
BEFORE UPDATE OF semester ON schedule_semester_selections
FOR EACH ROW
WHEN NEW.semester <> OLD.semester
BEGIN
    SELECT RAISE(ABORT, 'schedule_semester_selections.semester is immutable');
END;

CREATE TABLE IF NOT EXISTS transcription_jobs (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    job_key TEXT NOT NULL UNIQUE
        CHECK (
            job_key <> ''
            AND job_key NOT GLOB '*[^0-9A-Za-z_-]*'
        ),
    job_relpath TEXT NOT NULL
        CHECK (
            job_relpath <> ''
            AND job_relpath NOT LIKE '/%'
            AND instr(job_relpath, '\') = 0
            AND job_relpath <> '..'
            AND instr(job_relpath, '../') = 0
            AND instr(job_relpath, '/..') = 0
        ),
    requested_profile TEXT,
    requested_profile_version TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'processing', 'done', 'error', 'needs_review', 'canceled')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    config_json TEXT CHECK (config_json IS NULL OR json_valid(config_json)),
    manifest_json TEXT CHECK (manifest_json IS NULL OR json_valid(manifest_json)),
    error_code TEXT,
    error_message TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    archived_at TEXT,
    UNIQUE (id, recording_id)
);

CREATE TRIGGER IF NOT EXISTS transcription_jobs_job_key_immutable
BEFORE UPDATE OF job_key ON transcription_jobs
FOR EACH ROW
WHEN NEW.job_key <> OLD.job_key
BEGIN
    SELECT RAISE(ABORT, 'transcription_jobs.job_key is immutable');
END;

CREATE UNIQUE INDEX IF NOT EXISTS transcription_jobs_one_current_per_recording
ON transcription_jobs(recording_id)
WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS engine_runs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    recording_id INTEGER NOT NULL,
    engine_name TEXT NOT NULL,
    engine_version TEXT,
    provider TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('planned', 'running', 'succeeded', 'failed', 'superseded', 'canceled')),
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK (is_selected IN (0, 1)),
    params_json TEXT CHECK (params_json IS NULL OR json_valid(params_json)),
    metrics_json TEXT CHECK (metrics_json IS NULL OR json_valid(metrics_json)),
    stderr_relpath TEXT
        CHECK (
            stderr_relpath IS NULL
            OR (
                stderr_relpath <> ''
                AND stderr_relpath NOT LIKE '/%'
                AND instr(stderr_relpath, '\') = 0
                AND stderr_relpath <> '..'
                AND instr(stderr_relpath, '../') = 0
                AND instr(stderr_relpath, '/..') = 0
            )
        ),
    log_relpath TEXT
        CHECK (
            log_relpath IS NULL
            OR (
                log_relpath <> ''
                AND log_relpath NOT LIKE '/%'
                AND instr(log_relpath, '\') = 0
                AND log_relpath <> '..'
                AND instr(log_relpath, '../') = 0
                AND instr(log_relpath, '/..') = 0
            )
        ),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    archived_at TEXT,
    FOREIGN KEY (job_id, recording_id) REFERENCES transcription_jobs(id, recording_id) ON DELETE CASCADE,
    UNIQUE (id, job_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS engine_runs_one_selected_per_job
ON engine_runs(job_id)
WHERE is_selected = 1;

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    recording_id INTEGER NOT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
    event_type TEXT NOT NULL,
    event_json TEXT CHECK (event_json IS NULL OR json_valid(event_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id, recording_id) REFERENCES transcription_jobs(id, recording_id) ON DELETE CASCADE,
    UNIQUE (job_id, event_seq)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL,
    engine_run_id INTEGER,
    artifact_kind TEXT NOT NULL
        CHECK (
            artifact_kind IN (
                'source_copy',
                'transcript_raw_text',
                'transcript_segments_json',
                'quality_scorecard',
                'correction_text',
                'correction_json',
                'summary_markdown',
                'summary_json',
                'metadata',
                'log',
                'other'
            )
        ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    path_rel TEXT NOT NULL
        CHECK (
            path_rel <> ''
            AND path_rel NOT LIKE '/%'
            AND instr(path_rel, '\') = 0
            AND path_rel <> '..'
            AND instr(path_rel, '../') = 0
            AND instr(path_rel, '/..') = 0
        ),
    content_sha256 TEXT,
    bytes INTEGER CHECK (bytes IS NULL OR bytes >= 0),
    mime_type TEXT,
    is_latest INTEGER NOT NULL DEFAULT 1 CHECK (is_latest IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TEXT,
    FOREIGN KEY (job_id, recording_id) REFERENCES transcription_jobs(id, recording_id) ON DELETE CASCADE,
    FOREIGN KEY (engine_run_id, job_id) REFERENCES engine_runs(id, job_id) ON DELETE RESTRICT,
    UNIQUE (id, recording_id),
    UNIQUE (id, job_id, recording_id),
    UNIQUE (job_id, artifact_kind, revision),
    UNIQUE (job_id, path_rel),
    UNIQUE (recording_id, path_rel)
);

CREATE UNIQUE INDEX IF NOT EXISTS artifacts_one_latest_kind_per_job
ON artifacts(job_id, artifact_kind)
WHERE is_latest = 1 AND archived_at IS NULL;

CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    job_id INTEGER,
    artifact_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('open', 'triaged', 'resolved', 'dismissed')),
    severity TEXT CHECK (severity IS NULL OR severity IN ('low', 'medium', 'high')),
    reason_code TEXT NOT NULL,
    detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    CHECK (artifact_id IS NULL OR job_id IS NOT NULL),
    FOREIGN KEY (job_id, recording_id) REFERENCES transcription_jobs(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id, recording_id) REFERENCES artifacts(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id, job_id, recording_id)
        REFERENCES artifacts(id, job_id, recording_id) ON DELETE RESTRICT,
    UNIQUE (id, recording_id)
);

CREATE TABLE IF NOT EXISTS recording_classification_proposals (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL
        REFERENCES recordings(id) ON DELETE CASCADE,
    schedule_entry_id INTEGER,
    review_item_id INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('suggested', 'confirmed', 'rejected')),
    classification_reason TEXT NOT NULL
        CHECK (
            classification_reason IN (
                'unique_time_match',
                'recorded_at_missing',
                'recorded_at_invalid',
                'no_time_match',
                'ambiguous_time_match'
            )
        ),
    proposed_title TEXT NOT NULL CHECK (proposed_title <> '' AND length(proposed_title) <= 512),
    context_type TEXT NOT NULL
        CHECK (context_type IN ('general', 'class_session', 'daily_note', 'meeting', 'memo')),
    label TEXT CHECK (label IS NULL OR length(label) <= 256),
    semester TEXT NOT NULL CHECK (semester <> '' AND length(semester) <= 128),
    course_name TEXT CHECK (course_name IS NULL OR length(course_name) <= 256),
    course_code TEXT CHECK (course_code IS NULL OR length(course_code) <= 128),
    session_date TEXT,
    weekday TEXT
        CHECK (weekday IS NULL OR weekday IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')),
    start_time TEXT
        CHECK (
            start_time IS NULL
            OR (
                length(start_time) = 5
                AND start_time GLOB '[0-2][0-9]:[0-5][0-9]'
            )
        ),
    end_time TEXT
        CHECK (
            end_time IS NULL
            OR (
                length(end_time) = 5
                AND end_time GLOB '[0-2][0-9]:[0-5][0-9]'
            )
        ),
    period_label TEXT CHECK (period_label IS NULL OR length(period_label) <= 128),
    period_index INTEGER CHECK (period_index IS NULL OR period_index > 0),
    classroom TEXT CHECK (classroom IS NULL OR length(classroom) <= 256),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
    confirmation_plan_sha256 TEXT
        CHECK (
            confirmation_plan_sha256 IS NULL
            OR (
                length(confirmation_plan_sha256) = 64
                AND confirmation_plan_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (
            status = 'confirmed'
            AND confirmation_plan_sha256 IS NOT NULL
            AND confirmed_at IS NOT NULL
        )
        OR (
            status <> 'confirmed'
            AND confirmation_plan_sha256 IS NULL
            AND confirmed_at IS NULL
        )
    ),
    CHECK (
        classification_reason <> 'unique_time_match'
        OR schedule_entry_id IS NOT NULL
    ),
    FOREIGN KEY (schedule_entry_id, semester)
        REFERENCES schedule_entries(id, semester) ON DELETE RESTRICT,
    FOREIGN KEY (review_item_id, recording_id)
        REFERENCES review_items(id, recording_id) ON DELETE RESTRICT,
    UNIQUE (id, recording_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS
recording_classification_proposals_one_active_per_recording_semester
ON recording_classification_proposals(recording_id, semester)
WHERE status IN ('suggested', 'confirmed');

CREATE TRIGGER IF NOT EXISTS recording_classification_proposals_touch_updated_at
AFTER UPDATE OF
    recording_id,
    schedule_entry_id,
    review_item_id,
    status,
    classification_reason,
    proposed_title,
    context_type,
    label,
    semester,
    course_name,
    course_code,
    session_date,
    weekday,
    start_time,
    end_time,
    period_label,
    period_index,
    classroom,
    confidence,
    detail_json,
    confirmation_plan_sha256,
    confirmed_at
ON recording_classification_proposals
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE recording_classification_proposals
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS recording_title_proposals (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL
        REFERENCES recordings(id) ON DELETE CASCADE,
    transcript_artifact_id INTEGER NOT NULL,
    classification_proposal_id INTEGER,
    review_item_id INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('suggested', 'confirmed', 'rejected')),
    suggestion_reason TEXT NOT NULL
        CHECK (
            suggestion_reason IN (
                'schedule_content_match',
                'content_topic'
            )
        ),
    proposed_title TEXT NOT NULL
        CHECK (
            proposed_title <> ''
            AND length(proposed_title) <= 512
        ),
    confidence REAL NOT NULL
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    generator_version TEXT NOT NULL
        CHECK (generator_version = 'deterministic-keywords-v1'),
    detail_json TEXT NOT NULL
        CHECK (
            json_valid(detail_json)
            AND length(CAST(detail_json AS BLOB)) <= 4096
        ),
    confirmation_plan_sha256 TEXT
        CHECK (
            confirmation_plan_sha256 IS NULL
            OR (
                length(confirmation_plan_sha256) = 64
                AND confirmation_plan_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (
            status = 'confirmed'
            AND confirmation_plan_sha256 IS NOT NULL
            AND confirmed_at IS NOT NULL
        )
        OR (
            status <> 'confirmed'
            AND confirmation_plan_sha256 IS NULL
            AND confirmed_at IS NULL
        )
    ),
    CHECK (
        (
            suggestion_reason = 'schedule_content_match'
            AND classification_proposal_id IS NOT NULL
        )
        OR (
            suggestion_reason = 'content_topic'
            AND classification_proposal_id IS NULL
        )
    ),
    FOREIGN KEY (transcript_artifact_id, recording_id)
        REFERENCES artifacts(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (classification_proposal_id, recording_id)
        REFERENCES recording_classification_proposals(id, recording_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (review_item_id, recording_id)
        REFERENCES review_items(id, recording_id) ON DELETE RESTRICT,
    UNIQUE (id, recording_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS
recording_title_proposals_one_active_per_recording
ON recording_title_proposals(recording_id)
WHERE status IN ('suggested', 'confirmed');

CREATE TRIGGER IF NOT EXISTS recording_title_proposals_touch_updated_at
AFTER UPDATE OF
    recording_id,
    transcript_artifact_id,
    classification_proposal_id,
    review_item_id,
    status,
    suggestion_reason,
    proposed_title,
    confidence,
    generator_version,
    detail_json,
    confirmation_plan_sha256,
    confirmed_at
ON recording_title_proposals
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE recording_title_proposals
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS recording_classification_materializations (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE,
    recording_id INTEGER NOT NULL,
    previous_title_id INTEGER NOT NULL,
    previous_context_id INTEGER NOT NULL,
    materialized_title_id INTEGER NOT NULL UNIQUE,
    materialized_context_id INTEGER NOT NULL UNIQUE,
    confirmation_plan_sha256 TEXT NOT NULL
        CHECK (
            length(confirmation_plan_sha256) = 64
            AND confirmation_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    materialization_plan_sha256 TEXT NOT NULL UNIQUE
        CHECK (
            length(materialization_plan_sha256) = 64
            AND materialization_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    previous_manifest_sha256 TEXT NOT NULL
        CHECK (
            length(previous_manifest_sha256) = 64
            AND previous_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    materialized_manifest_sha256 TEXT NOT NULL
        CHECK (
            length(materialized_manifest_sha256) = 64
            AND materialized_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    plan_json TEXT NOT NULL
        CHECK (
            json_valid(plan_json)
            AND length(CAST(plan_json AS BLOB)) <= 65536
        ),
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'applied')),
    prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    CHECK (previous_title_id <> materialized_title_id),
    CHECK (previous_context_id <> materialized_context_id),
    CHECK (
        (state = 'prepared' AND applied_at IS NULL)
        OR (state = 'applied' AND applied_at IS NOT NULL)
    ),
    FOREIGN KEY (proposal_id, recording_id)
        REFERENCES recording_classification_proposals(id, recording_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (previous_title_id, recording_id)
        REFERENCES recording_titles(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (previous_context_id, recording_id)
        REFERENCES recording_contexts(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (materialized_title_id, recording_id)
        REFERENCES recording_titles(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (materialized_context_id, recording_id)
        REFERENCES recording_contexts(id, recording_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS
recording_classification_materializations_one_prepared_per_recording
ON recording_classification_materializations(recording_id)
WHERE state = 'prepared';

CREATE TRIGGER IF NOT EXISTS
recording_classification_materializations_identity_immutable
BEFORE UPDATE OF
    proposal_id,
    recording_id,
    previous_title_id,
    previous_context_id,
    materialized_title_id,
    materialized_context_id,
    confirmation_plan_sha256,
    materialization_plan_sha256,
    previous_manifest_sha256,
    materialized_manifest_sha256,
    plan_json,
    prepared_at
ON recording_classification_materializations
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'recording_classification_materializations identity is immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
recording_classification_materializations_state_transition
BEFORE UPDATE OF state, applied_at
ON recording_classification_materializations
FOR EACH ROW
WHEN NOT (
    OLD.state = 'prepared'
    AND OLD.applied_at IS NULL
    AND NEW.state = 'applied'
    AND NEW.applied_at IS NOT NULL
)
BEGIN
    SELECT RAISE(
        ABORT,
        'recording_classification_materializations only supports prepared to applied'
    );
END;

CREATE TABLE IF NOT EXISTS recording_title_materializations (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE,
    recording_id INTEGER NOT NULL,
    previous_title_id INTEGER NOT NULL,
    materialized_title_id INTEGER NOT NULL UNIQUE,
    confirmation_plan_sha256 TEXT NOT NULL
        CHECK (
            length(confirmation_plan_sha256) = 64
            AND confirmation_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    materialization_plan_sha256 TEXT NOT NULL UNIQUE
        CHECK (
            length(materialization_plan_sha256) = 64
            AND materialization_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    previous_manifest_sha256 TEXT NOT NULL
        CHECK (
            length(previous_manifest_sha256) = 64
            AND previous_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    materialized_manifest_sha256 TEXT NOT NULL
        CHECK (
            length(materialized_manifest_sha256) = 64
            AND materialized_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    plan_json TEXT NOT NULL
        CHECK (
            json_valid(plan_json)
            AND length(CAST(plan_json AS BLOB)) <= 65536
        ),
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'applied')),
    prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    CHECK (previous_title_id <> materialized_title_id),
    CHECK (
        (state = 'prepared' AND applied_at IS NULL)
        OR (state = 'applied' AND applied_at IS NOT NULL)
    ),
    FOREIGN KEY (proposal_id, recording_id)
        REFERENCES recording_title_proposals(id, recording_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (previous_title_id, recording_id)
        REFERENCES recording_titles(id, recording_id) ON DELETE RESTRICT,
    FOREIGN KEY (materialized_title_id, recording_id)
        REFERENCES recording_titles(id, recording_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS
recording_title_materializations_one_prepared_per_recording
ON recording_title_materializations(recording_id)
WHERE state = 'prepared';

CREATE TRIGGER IF NOT EXISTS
recording_title_materializations_identity_immutable
BEFORE UPDATE OF
    id,
    proposal_id,
    recording_id,
    previous_title_id,
    materialized_title_id,
    confirmation_plan_sha256,
    materialization_plan_sha256,
    previous_manifest_sha256,
    materialized_manifest_sha256,
    plan_json,
    prepared_at
ON recording_title_materializations
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'recording_title_materializations identity is immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS
recording_title_materializations_state_transition
BEFORE UPDATE OF state, applied_at
ON recording_title_materializations
FOR EACH ROW
WHEN NOT (
    OLD.state = 'prepared'
    AND OLD.applied_at IS NULL
    AND NEW.state = 'applied'
    AND NEW.applied_at IS NOT NULL
)
BEGIN
    SELECT RAISE(
        ABORT,
        'recording_title_materializations only supports prepared to applied'
    );
END;

CREATE TRIGGER IF NOT EXISTS
recording_title_materializations_delete_forbidden
BEFORE DELETE ON recording_title_materializations
FOR EACH ROW
BEGIN
    SELECT RAISE(
        ABORT,
        'recording_title_materializations rows cannot be deleted'
    );
END;

CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY,
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN ('recording', 'job', 'review_item')),
    aggregate_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'discarded')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dedupe_key)
);

CREATE TABLE IF NOT EXISTS legacy_import_map (
    id INTEGER PRIMARY KEY,
    legacy_kind TEXT NOT NULL
        CHECK (legacy_kind IN ('job', 'delivery', 'artifact')),
    legacy_key TEXT NOT NULL CHECK (legacy_key <> ''),
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL CHECK (source_fingerprint <> ''),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    legacy_snapshot_json TEXT CHECK (legacy_snapshot_json IS NULL OR json_valid(legacy_snapshot_json)),
    UNIQUE (legacy_kind, legacy_key)
);

CREATE TABLE IF NOT EXISTS archive_evidence_cases (
    id INTEGER PRIMARY KEY,
    case_key TEXT NOT NULL UNIQUE
        CHECK (
            case_key <> ''
            AND case_key NOT GLOB '*[^0-9A-Za-z_-]*'
        ),
    legacy_delivery_key TEXT NOT NULL UNIQUE
        CHECK (legacy_delivery_key <> ''),
    logical_stem TEXT NOT NULL CHECK (logical_stem <> ''),
    subject_abbr TEXT,
    review_status TEXT NOT NULL DEFAULT 'open'
        CHECK (review_status IN ('open', 'triaged', 'resolved', 'dismissed')),
    promoted_recording_id INTEGER
        REFERENCES recordings(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TRIGGER IF NOT EXISTS archive_evidence_cases_case_key_immutable
BEFORE UPDATE OF case_key, legacy_delivery_key, logical_stem
ON archive_evidence_cases
FOR EACH ROW
WHEN
    NEW.case_key <> OLD.case_key
    OR NEW.legacy_delivery_key <> OLD.legacy_delivery_key
    OR NEW.logical_stem <> OLD.logical_stem
BEGIN
    SELECT RAISE(ABORT, 'archive_evidence_cases identity is immutable');
END;

CREATE TABLE IF NOT EXISTS archive_evidence_captures (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES archive_evidence_cases(id) ON DELETE CASCADE,
    capture_key TEXT NOT NULL UNIQUE
        CHECK (
            capture_key <> ''
            AND capture_key NOT GLOB '*[^0-9A-Za-z_-]*'
        ),
    reconciliation_classification TEXT NOT NULL
        CHECK (
            reconciliation_classification IN (
                'verified_delivered',
                'verified_correction_only',
                'manual_review',
                'blocked'
            )
        ),
    legacy_database_sha256 TEXT NOT NULL
        CHECK (
            length(legacy_database_sha256) = 64
            AND legacy_database_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    source_fingerprint TEXT NOT NULL
        CHECK (
            length(source_fingerprint) = 64
            AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
    plan_sha256 TEXT NOT NULL
        CHECK (
            length(plan_sha256) = 64
            AND plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    manifest_relpath TEXT NOT NULL
        CHECK (
            manifest_relpath <> ''
            AND manifest_relpath NOT LIKE '/%'
            AND instr(manifest_relpath, '\') = 0
            AND manifest_relpath <> '..'
            AND instr(manifest_relpath, '../') = 0
            AND instr(manifest_relpath, '/..') = 0
        ),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, case_id),
    UNIQUE (case_id, capture_key),
    UNIQUE (case_id, manifest_relpath)
);

CREATE TRIGGER IF NOT EXISTS archive_evidence_captures_capture_key_immutable
BEFORE UPDATE ON archive_evidence_captures
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive_evidence_captures rows are immutable');
END;

CREATE TABLE IF NOT EXISTS archive_evidence_revisions (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES archive_evidence_cases(id) ON DELETE CASCADE,
    artifact_kind TEXT NOT NULL
        CHECK (
            artifact_kind IN (
                'correction_text',
                'correction_json',
                'summary_markdown'
            )
        ),
    content_sha256 TEXT NOT NULL
        CHECK (
            length(content_sha256) = 64
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    mime_type TEXT,
    path_rel TEXT NOT NULL
        CHECK (
            path_rel <> ''
            AND path_rel NOT LIKE '/%'
            AND instr(path_rel, '\') = 0
            AND path_rel <> '..'
            AND instr(path_rel, '../') = 0
            AND instr(path_rel, '/..') = 0
        ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, case_id),
    UNIQUE (id, case_id, artifact_kind),
    UNIQUE (case_id, artifact_kind, content_sha256),
    UNIQUE (case_id, path_rel)
);

CREATE TABLE IF NOT EXISTS archive_evidence_observations (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    source_role TEXT NOT NULL
        CHECK (source_role IN ('current_gh', 'current_obsidian', 'historical')),
    source_root_label TEXT NOT NULL
        CHECK (
            source_root_label <> ''
            AND source_root_label NOT GLOB '*[^0-9A-Za-z_-]*'
        ),
    source_relpath TEXT NOT NULL
        CHECK (
            source_relpath <> ''
            AND source_relpath NOT LIKE '/%'
            AND instr(source_relpath, '\') = 0
            AND source_relpath <> '..'
            AND instr(source_relpath, '../') = 0
            AND instr(source_relpath, '/..') = 0
        ),
    claimed_sha256 TEXT
        CHECK (
            claimed_sha256 IS NULL
            OR (
                length(claimed_sha256) = 64
                AND claimed_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    observed_sha256 TEXT NOT NULL
        CHECK (
            length(observed_sha256) = 64
            AND observed_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    relationship TEXT NOT NULL
        CHECK (
            relationship IN (
                'matches_ledger',
                'differs_from_ledger',
                'unclaimed',
                'unexpected_current'
            )
        ),
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (capture_id, case_id)
        REFERENCES archive_evidence_captures(id, case_id) ON DELETE CASCADE,
    FOREIGN KEY (revision_id, case_id)
        REFERENCES archive_evidence_revisions(id, case_id) ON DELETE RESTRICT,
    UNIQUE (
        capture_id,
        revision_id,
        source_role,
        source_root_label,
        source_relpath
    )
);

CREATE TRIGGER IF NOT EXISTS archive_evidence_revisions_immutable
BEFORE UPDATE ON archive_evidence_revisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive_evidence_revisions rows are immutable');
END;

CREATE TRIGGER IF NOT EXISTS archive_evidence_observations_immutable
BEFORE UPDATE ON archive_evidence_observations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive_evidence_observations rows are immutable');
END;

CREATE TABLE IF NOT EXISTS archive_evidence_canonical_selections (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES archive_evidence_cases(id) ON DELETE CASCADE,
    revision_id INTEGER NOT NULL,
    artifact_kind TEXT NOT NULL
        CHECK (
            artifact_kind IN (
                'correction_text',
                'correction_json',
                'summary_markdown'
            )
        ),
    promotion_plan_sha256 TEXT NOT NULL
        CHECK (
            length(promotion_plan_sha256) = 64
            AND promotion_plan_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (revision_id, case_id, artifact_kind)
        REFERENCES archive_evidence_revisions(id, case_id, artifact_kind)
        ON DELETE RESTRICT,
    UNIQUE (case_id, artifact_kind),
    UNIQUE (case_id, revision_id)
);

CREATE TRIGGER IF NOT EXISTS archive_evidence_canonical_selections_immutable
BEFORE UPDATE ON archive_evidence_canonical_selections
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive_evidence_canonical_selections rows are immutable');
END;

-- The raw SQL intentionally leaves the checksum unstamped because embedding the
-- digest in this file would make the digest self-referential. apply_migration()
-- computes the digest of these exact UTF-8 bytes and stamps it after the DDL
-- signature has been verified. Keeping NULL here also preserves raw SQL
-- idempotency for schema inspection and tests.
INSERT OR IGNORE INTO schema_migrations(version, checksum_sha256)
VALUES ('storage_v2/0001_recording_store', NULL);

COMMIT;
