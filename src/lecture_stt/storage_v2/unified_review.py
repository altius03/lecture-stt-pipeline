from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


SCHEMA_VERSION = "storage-v2/unified-review-feed@2"
_LIMIT_MAX = 200
_OFFSET_MAX = 100_000
_TIMESTAMP_MAX = 64
_ID_MAX = 512
_TITLE_MAX = 1024
_LABEL_MAX = 512
_META_MAX = 1024
_HREF_MAX = 1024
_JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_CANONICAL_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
_SOURCE_LABELS = {
    "archive": "archive",
    "timetable": "classification",
    "title": "title",
    "recording": "recording",
}
_SOURCE_DISABLED_NOTES = {
    "archive": "archive_review_disabled",
    "timetable": "timetable_api_disabled",
    "title": "title_review_disabled",
    "recording": "recording_library_disabled",
}
_DB_UNAVAILABLE_NOTE = "storage_v2_db_unavailable"


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _validate_bool(value: bool, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _validate_limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 <= limit <= _LIMIT_MAX
    ):
        raise ValueError(f"limit must be between 0 and {_LIMIT_MAX}")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= _OFFSET_MAX
    ):
        raise ValueError(f"offset must be between 0 and {_OFFSET_MAX}")
    return limit, offset


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    required: bool = True,
) -> str:
    normalized = _normalize_text(value)
    if required and normalized == "":
        raise ValueError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise ValueError(f"{field} exceeds the {max_length}-character limit")
    return normalized


def _validated_timestamp(value: Any, *, field: str) -> str:
    timestamp = _bounded_text(
        value,
        field=field,
        max_length=_TIMESTAMP_MAX,
    )
    try:
        datetime.fromisoformat(
            timestamp[:-1] + "+00:00"
            if timestamp.endswith("Z")
            else timestamp
        )
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO timestamp") from exc
    return timestamp


def _validated_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not 0 <= value <= _JS_SAFE_INTEGER_MAX:
        raise ValueError(
            f"{field} must be between 0 and {_JS_SAFE_INTEGER_MAX}"
        )
    return value


def _source_payload(
    source: str,
    *,
    ready: bool,
    visible_count: int,
    total_count: int | None,
    truncated: bool,
    note: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "label": _SOURCE_LABELS[source],
        "status": "ready" if ready else "unavailable",
        "available": ready,
        "visible_count": visible_count,
        "total_count": total_count,
        "truncated": truncated,
        "note": _bounded_text(
            note,
            field=f"sources[{source}].note",
            max_length=_META_MAX,
        ),
        "error": None,
    }


def _unavailable_payload(
    *,
    limit: int,
    offset: int,
    archive_enabled: bool,
    timetable_enabled: bool,
    title_enabled: bool,
    recording_enabled: bool,
    db_available: bool,
) -> dict[str, Any]:
    def payload_for(source: str, enabled: bool) -> dict[str, Any]:
        note = (
            _DB_UNAVAILABLE_NOTE
            if enabled and not db_available
            else _SOURCE_DISABLED_NOTES[source]
        )
        return _source_payload(
            source,
            ready=False,
            visible_count=0,
            total_count=None,
            truncated=False,
            note=note,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "filters": {
            "limit": limit,
            "offset": offset,
        },
        "total": 0,
        "items": [],
        "sources": [
            payload_for("archive", archive_enabled),
            payload_for("timetable", timetable_enabled),
            payload_for("title", title_enabled),
            payload_for("recording", recording_enabled),
        ],
    }


def _archive_items_cte(enabled: bool) -> str:
    if not enabled:
        return """
        archive_items AS (
            SELECT
                CAST(NULL AS TEXT) AS id,
                CAST(NULL AS TEXT) AS source,
                CAST(NULL AS TEXT) AS title,
                CAST(NULL AS TEXT) AS state_label,
                CAST(NULL AS TEXT) AS reason_label,
                CAST(NULL AS TEXT) AS timestamp,
                CAST(NULL AS TEXT) AS meta_label,
                CAST(NULL AS TEXT) AS href,
                0 AS sort_group,
                0 AS source_order
            WHERE 0
        )
        """
    return """
    archive_items AS (
        SELECT
            'archive:' || c.case_key AS id,
            'archive' AS source,
            CASE
                WHEN c.subject_abbr IS NOT NULL AND c.subject_abbr <> ''
                    THEN c.subject_abbr || ' · ' || c.logical_stem
                ELSE c.logical_stem
            END AS title,
            CASE c.review_status
                WHEN 'open' THEN '열림'
                WHEN 'triaged' THEN '분류 완료'
                ELSE c.review_status
            END AS state_label,
            COALESCE(
                CASE (
                    SELECT latest.reconciliation_classification
                    FROM archive_evidence_captures AS latest
                    WHERE latest.case_id = c.id
                    ORDER BY latest.captured_at DESC, latest.id DESC
                    LIMIT 1
                )
                    WHEN 'manual_review' THEN 'latest pending'
                    WHEN 'verified_correction_only' THEN 'latest candidate'
                    WHEN 'verified_delivered' THEN 'latest matched'
                    WHEN 'blocked' THEN 'latest blocked'
                    ELSE
                        'latest ' || (
                            SELECT latest.reconciliation_classification
                            FROM archive_evidence_captures AS latest
                            WHERE latest.case_id = c.id
                            ORDER BY latest.captured_at DESC, latest.id DESC
                            LIMIT 1
                        )
                END,
                'latest classification 없음'
            ) AS reason_label,
            c.updated_at AS timestamp,
            CASE
                WHEN promoted.storage_key IS NOT NULL
                    THEN promoted.storage_key
                        || ' · capture '
                        || COUNT(DISTINCT capture.id)
                        || ' · revision '
                        || COUNT(DISTINCT revision.id)
                ELSE
                    'capture '
                    || COUNT(DISTINCT capture.id)
                    || ' · revision '
                    || COUNT(DISTINCT revision.id)
            END AS meta_label,
            '#review/archive/' || c.case_key AS href,
            CASE c.review_status WHEN 'triaged' THEN 1 ELSE 0 END AS sort_group,
            0 AS source_order
        FROM archive_evidence_cases AS c
        LEFT JOIN recordings AS promoted
          ON promoted.id = c.promoted_recording_id
        LEFT JOIN archive_evidence_captures AS capture
          ON capture.case_id = c.id
        LEFT JOIN archive_evidence_revisions AS revision
          ON revision.case_id = c.id
        WHERE c.review_status IN ('open', 'triaged')
        GROUP BY c.id
    )
    """


def _timetable_items_cte(enabled: bool) -> str:
    if not enabled:
        return """
        timetable_items AS (
            SELECT
                CAST(NULL AS TEXT) AS id,
                CAST(NULL AS TEXT) AS source,
                CAST(NULL AS TEXT) AS title,
                CAST(NULL AS TEXT) AS state_label,
                CAST(NULL AS TEXT) AS reason_label,
                CAST(NULL AS TEXT) AS timestamp,
                CAST(NULL AS TEXT) AS meta_label,
                CAST(NULL AS TEXT) AS href,
                0 AS sort_group,
                1 AS source_order
            WHERE 0
        )
        """
    return """
    timetable_items AS (
        SELECT
            'timetable:' || proposal.id AS id,
            'timetable' AS source,
            proposal.proposed_title AS title,
            '검토 대기' AS state_label,
            CASE proposal.classification_reason
                WHEN 'unique_time_match' THEN '유일 시간 일치'
                WHEN 'recorded_at_missing' THEN '녹음 시각 없음'
                WHEN 'recorded_at_invalid' THEN '녹음 시각 오류'
                WHEN 'no_time_match' THEN '시간표 후보 없음'
                WHEN 'ambiguous_time_match' THEN '복수 후보'
                ELSE proposal.classification_reason
            END AS reason_label,
            proposal.updated_at AS timestamp,
            recording.storage_key AS meta_label,
            '#review/timetable/' || proposal.id AS href,
            0 AS sort_group,
            1 AS source_order
        FROM recording_classification_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        WHERE proposal.status = 'suggested'
          AND recording.archived_at IS NULL
    )
    """


def _title_items_cte(enabled: bool) -> str:
    if not enabled:
        return """
        title_items AS (
            SELECT
                CAST(NULL AS TEXT) AS id,
                CAST(NULL AS TEXT) AS source,
                CAST(NULL AS TEXT) AS title,
                CAST(NULL AS TEXT) AS state_label,
                CAST(NULL AS TEXT) AS reason_label,
                CAST(NULL AS TEXT) AS timestamp,
                CAST(NULL AS TEXT) AS meta_label,
                CAST(NULL AS TEXT) AS href,
                0 AS sort_group,
                2 AS source_order
            WHERE 0
        )
        """
    return """
    title_items AS (
        SELECT
            'title:' || proposal.id AS id,
            'title' AS source,
            proposal.proposed_title AS title,
            '검토 대기' AS state_label,
            CASE proposal.suggestion_reason
                WHEN 'schedule_content_match' THEN '시간표+본문 일치'
                WHEN 'content_topic' THEN '본문 주제'
                ELSE proposal.suggestion_reason
            END AS reason_label,
            proposal.updated_at AS timestamp,
            recording.storage_key || ' · 제안 제목 검토' AS meta_label,
            '#review/title/' || proposal.id AS href,
            0 AS sort_group,
            2 AS source_order
        FROM recording_title_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        WHERE proposal.status = 'suggested'
          AND recording.archived_at IS NULL
    )
    """


def _suggested_timetable_review_links_cte(enabled: bool) -> str:
    if not enabled:
        return """
        suggested_timetable_review_links AS (
            SELECT CAST(NULL AS INTEGER) AS review_item_id
            WHERE 0
        )
        """
    return """
    suggested_timetable_review_links AS (
        SELECT DISTINCT proposal.review_item_id AS review_item_id
        FROM recording_classification_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        WHERE proposal.status = 'suggested'
          AND recording.archived_at IS NULL
    )
    """


def _suggested_title_review_links_cte(enabled: bool) -> str:
    if not enabled:
        return """
        suggested_title_review_links AS (
            SELECT CAST(NULL AS INTEGER) AS review_item_id
            WHERE 0
        )
        """
    return """
    suggested_title_review_links AS (
        SELECT DISTINCT proposal.review_item_id AS review_item_id
        FROM recording_title_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        WHERE proposal.status = 'suggested'
          AND recording.archived_at IS NULL
    )
    """


def _suggested_review_links_cte(
    *,
    timetable_enabled: bool,
    title_enabled: bool,
) -> str:
    if not timetable_enabled and not title_enabled:
        return """
        suggested_review_links AS (
            SELECT CAST(NULL AS INTEGER) AS review_item_id
            WHERE 0
        )
        """
    union_parts: list[str] = []
    if timetable_enabled:
        union_parts.append(
            "SELECT review_item_id FROM suggested_timetable_review_links"
        )
    if title_enabled:
        union_parts.append(
            "SELECT review_item_id FROM suggested_title_review_links"
        )
    return f"""
    suggested_review_links AS (
        SELECT DISTINCT review_item_id
        FROM (
            {" UNION ALL ".join(union_parts)}
        )
    )
    """


def _recording_rollup_cte(enabled: bool) -> str:
    if not enabled:
        return """
        recording_rollup AS (
            SELECT
                CAST(NULL AS INTEGER) AS recording_id,
                CAST(NULL AS TEXT) AS storage_key,
                CAST(NULL AS TEXT) AS display_name,
                CAST(NULL AS TEXT) AS timestamp,
                0 AS open_review_count,
                0 AS timetable_linked_review_count,
                0 AS title_linked_review_count,
                0 AS linked_review_count,
                0 AS remaining_review_count
            WHERE 0
        )
        """
    return """
    recording_rollup AS (
        SELECT
            recording.id AS recording_id,
            recording.storage_key AS storage_key,
            COALESCE(title.title, recording.original_name_nfc) AS display_name,
            COALESCE(
                recording.recorded_at,
                recording.received_at,
                recording.created_at
            ) AS timestamp,
            COUNT(DISTINCT review.id)
                AS open_review_count,
            COUNT(
                DISTINCT CASE
                    WHEN timetable_linked.review_item_id IS NOT NULL
                        THEN review.id
                    ELSE NULL
                END
            ) AS timetable_linked_review_count,
            COUNT(
                DISTINCT CASE
                    WHEN title_linked.review_item_id IS NOT NULL
                        THEN review.id
                    ELSE NULL
                END
            ) AS title_linked_review_count,
            COUNT(
                DISTINCT CASE
                    WHEN linked.review_item_id IS NOT NULL
                        THEN review.id
                    ELSE NULL
                END
            )
                AS linked_review_count,
            COUNT(
                DISTINCT CASE
                    WHEN review.id IS NOT NULL
                     AND linked.review_item_id IS NULL
                        THEN review.id
                    ELSE NULL
                END
            ) AS remaining_review_count
        FROM recordings AS recording
        LEFT JOIN recording_titles AS title
          ON title.recording_id = recording.id
         AND title.is_current = 1
        LEFT JOIN review_items AS review
          ON review.recording_id = recording.id
         AND review.status IN ('open', 'triaged')
        LEFT JOIN suggested_timetable_review_links AS timetable_linked
          ON timetable_linked.review_item_id = review.id
        LEFT JOIN suggested_title_review_links AS title_linked
          ON title_linked.review_item_id = review.id
        LEFT JOIN suggested_review_links AS linked
          ON linked.review_item_id = review.id
        WHERE recording.archived_at IS NULL
        GROUP BY recording.id
    )
    """


def _recording_items_cte(enabled: bool) -> str:
    if not enabled:
        return """
        recording_items AS (
            SELECT
                CAST(NULL AS TEXT) AS id,
                CAST(NULL AS TEXT) AS source,
                CAST(NULL AS TEXT) AS title,
                CAST(NULL AS TEXT) AS state_label,
                CAST(NULL AS TEXT) AS reason_label,
                CAST(NULL AS TEXT) AS timestamp,
                CAST(NULL AS TEXT) AS meta_label,
                CAST(NULL AS TEXT) AS href,
                0 AS sort_group,
                3 AS source_order
            WHERE 0
        )
        """
    return """
    recording_items AS (
        SELECT
            'recording:' || storage_key AS id,
            'recording' AS source,
            display_name AS title,
            '열린 검토 ' || remaining_review_count || '건' AS state_label,
            CASE
                WHEN linked_review_count > 0
                    THEN 'distinct linked suggested review '
                        || linked_review_count
                        || '건 제외 후 남은 review'
                ELSE 'distinct linked suggested review 차감 없음'
            END AS reason_label,
            timestamp,
            storage_key || ' · 원본 ' || open_review_count || '건' AS meta_label,
            '#review/recording/' || storage_key AS href,
            0 AS sort_group,
            3 AS source_order
        FROM recording_rollup
        WHERE remaining_review_count > 0
    )
    """


def _unified_items_cte() -> str:
    return """
    unified_items AS (
        SELECT * FROM archive_items
        UNION ALL
        SELECT * FROM timetable_items
        UNION ALL
        SELECT * FROM title_items
        UNION ALL
        SELECT * FROM recording_items
    )
    """


def _common_ctes(
    *,
    archive_enabled: bool,
    timetable_enabled: bool,
    title_enabled: bool,
    recording_enabled: bool,
) -> str:
    return ",\n".join(
        (
            _archive_items_cte(archive_enabled).strip(),
            _timetable_items_cte(timetable_enabled).strip(),
            _title_items_cte(title_enabled).strip(),
            _suggested_timetable_review_links_cte(
                timetable_enabled
            ).strip(),
            _suggested_title_review_links_cte(title_enabled).strip(),
            _suggested_review_links_cte(
                timetable_enabled=timetable_enabled,
                title_enabled=title_enabled,
            ).strip(),
            _recording_rollup_cte(recording_enabled).strip(),
            _recording_items_cte(recording_enabled).strip(),
            _unified_items_cte().strip(),
        )
    )


def _count_query(
    *,
    archive_enabled: bool,
    timetable_enabled: bool,
    title_enabled: bool,
    recording_enabled: bool,
) -> str:
    return f"""
    WITH
    {_common_ctes(
        archive_enabled=archive_enabled,
        timetable_enabled=timetable_enabled,
        title_enabled=title_enabled,
        recording_enabled=recording_enabled,
    )}
    SELECT
        (SELECT COUNT(*) FROM archive_items) AS archive_total,
        (SELECT COUNT(*) FROM timetable_items) AS timetable_total,
        (SELECT COUNT(*) FROM title_items) AS title_total,
        (SELECT COUNT(*) FROM recording_items) AS recording_total,
        (SELECT COALESCE(SUM(open_review_count), 0) FROM recording_rollup)
            AS recording_open_review_total,
        (SELECT COALESCE(SUM(timetable_linked_review_count), 0) FROM recording_rollup)
            AS recording_timetable_linked_review_total,
        (SELECT COALESCE(SUM(title_linked_review_count), 0) FROM recording_rollup)
            AS recording_title_linked_review_total,
        (SELECT COALESCE(SUM(linked_review_count), 0) FROM recording_rollup)
            AS recording_linked_review_total,
        (SELECT COALESCE(SUM(remaining_review_count), 0) FROM recording_rollup)
            AS recording_remaining_review_total,
        (
            SELECT COUNT(*)
            FROM unified_items
            WHERE timestamp IS NULL OR unixepoch(timestamp) IS NULL
        ) AS invalid_timestamp_total
    """


def _page_query(
    *,
    archive_enabled: bool,
    timetable_enabled: bool,
    title_enabled: bool,
    recording_enabled: bool,
) -> str:
    return f"""
    WITH
    {_common_ctes(
        archive_enabled=archive_enabled,
        timetable_enabled=timetable_enabled,
        title_enabled=title_enabled,
        recording_enabled=recording_enabled,
    )}
    SELECT
        id,
        source,
        title,
        state_label,
        reason_label,
        timestamp,
        meta_label,
        href
    FROM unified_items
    ORDER BY
        sort_group ASC,
        COALESCE(unixepoch(timestamp), 0) DESC,
        source_order ASC,
        id ASC
    LIMIT ? OFFSET ?
    """


def _validated_page_item(row: sqlite3.Row) -> dict[str, str]:
    source = _bounded_text(
        row["source"],
        field="items[].source",
        max_length=32,
    )
    if source not in _SOURCE_LABELS:
        raise ValueError(
            "items[].source must be archive, timetable, title, or recording"
        )
    item_id = _bounded_text(
        row["id"],
        field="items[].id",
        max_length=_ID_MAX,
    )
    prefix, separator, identity = item_id.partition(":")
    if separator != ":" or prefix != source or not identity:
        raise ValueError("items[].id must match its source identity")
    if source in {"timetable", "title"}:
        if not _POSITIVE_INTEGER_RE.fullmatch(identity):
            raise ValueError(
                "items[].proposal identity must be a positive integer"
            )
        if int(identity) > _JS_SAFE_INTEGER_MAX:
            raise ValueError(
                "items[].proposal identity exceeds the public integer limit"
            )
    elif not _CANONICAL_KEY_RE.fullmatch(identity):
        raise ValueError("items[].identity must use the canonical ASCII key format")
    href = _bounded_text(
        row["href"],
        field="items[].href",
        max_length=_HREF_MAX,
    )
    if href != f"#review/{source}/{identity}":
        raise ValueError("items[].href must match its canonical item identity")
    return {
        "id": item_id,
        "source": source,
        "title": _bounded_text(
            row["title"],
            field="items[].title",
            max_length=_TITLE_MAX,
        ),
        "state_label": _bounded_text(
            row["state_label"],
            field="items[].state_label",
            max_length=_LABEL_MAX,
        ),
        "reason_label": _bounded_text(
            row["reason_label"],
            field="items[].reason_label",
            max_length=_LABEL_MAX,
        ),
        "timestamp": _validated_timestamp(
            row["timestamp"],
            field="items[].timestamp",
        ),
        "meta_label": _bounded_text(
            row["meta_label"],
            field="items[].meta_label",
            max_length=_META_MAX,
        ),
        "href": href,
    }


def _recording_note(
    *,
    recording_total: int,
    visible_count: int,
    open_review_total: int,
    timetable_linked_review_total: int,
    title_linked_review_total: int,
    linked_review_total: int,
    remaining_review_total: int,
) -> str:
    return (
        f"전체 actionable recording {recording_total}건 중 현재 page {visible_count}건 표시 · "
        f"전체 non-archived 원본 open review {open_review_total}건에서 full-table distinct "
        f"timetable linked suggested review {timetable_linked_review_total}건 + "
        f"title linked suggested review {title_linked_review_total}건 "
        f"(combined distinct {linked_review_total}건) 제외 후 "
        f"remaining review {remaining_review_total}건"
    )


def read_unified_review_feed(
    db_path: Path | str,
    *,
    archive_enabled: bool,
    timetable_enabled: bool,
    title_enabled: bool,
    recording_enabled: bool,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    archive_enabled = _validate_bool(archive_enabled, field="archive_enabled")
    timetable_enabled = _validate_bool(
        timetable_enabled,
        field="timetable_enabled",
    )
    title_enabled = _validate_bool(
        title_enabled,
        field="title_enabled",
    )
    recording_enabled = _validate_bool(
        recording_enabled,
        field="recording_enabled",
    )
    limit, offset = _validate_limit_offset(limit, offset)

    if not any(
        (
            archive_enabled,
            timetable_enabled,
            title_enabled,
            recording_enabled,
        )
    ):
        return _unavailable_payload(
            limit=limit,
            offset=offset,
            archive_enabled=False,
            timetable_enabled=False,
            title_enabled=False,
            recording_enabled=False,
            db_available=False,
        )

    path = Path(db_path).expanduser()
    if not path.exists():
        return _unavailable_payload(
            limit=limit,
            offset=offset,
            archive_enabled=archive_enabled,
            timetable_enabled=timetable_enabled,
            title_enabled=title_enabled,
            recording_enabled=recording_enabled,
            db_available=False,
        )

    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN")
        count_row = conn.execute(
            _count_query(
                archive_enabled=archive_enabled,
                timetable_enabled=timetable_enabled,
                title_enabled=title_enabled,
                recording_enabled=recording_enabled,
            )
        ).fetchone()
        if count_row is None:
            raise RuntimeError("unified review count query returned no row")

        archive_total = _validated_count(
            count_row["archive_total"],
            field="archive_total",
        )
        timetable_total = _validated_count(
            count_row["timetable_total"],
            field="timetable_total",
        )
        title_total = _validated_count(
            count_row["title_total"],
            field="title_total",
        )
        recording_total = _validated_count(
            count_row["recording_total"],
            field="recording_total",
        )
        recording_open_review_total = _validated_count(
            count_row["recording_open_review_total"],
            field="recording_open_review_total",
        )
        recording_timetable_linked_review_total = _validated_count(
            count_row["recording_timetable_linked_review_total"],
            field="recording_timetable_linked_review_total",
        )
        recording_title_linked_review_total = _validated_count(
            count_row["recording_title_linked_review_total"],
            field="recording_title_linked_review_total",
        )
        recording_linked_review_total = _validated_count(
            count_row["recording_linked_review_total"],
            field="recording_linked_review_total",
        )
        recording_remaining_review_total = _validated_count(
            count_row["recording_remaining_review_total"],
            field="recording_remaining_review_total",
        )
        invalid_timestamp_total = _validated_count(
            count_row["invalid_timestamp_total"],
            field="invalid_timestamp_total",
        )
        if invalid_timestamp_total:
            raise ValueError("unified review timestamps must be valid ISO timestamps")
        total = _validated_count(
            archive_total + timetable_total + title_total + recording_total,
            field="total",
        )

        page_rows = conn.execute(
            _page_query(
                archive_enabled=archive_enabled,
                timetable_enabled=timetable_enabled,
                title_enabled=title_enabled,
                recording_enabled=recording_enabled,
            ),
            (limit, offset),
        ).fetchall()
        items = [_validated_page_item(row) for row in page_rows]

        visible_counts = {
            "archive": 0,
            "timetable": 0,
            "title": 0,
            "recording": 0,
        }
        for item in items:
            visible_counts[item["source"]] += 1

        sources = [
            _source_payload(
                "archive",
                ready=archive_enabled,
                visible_count=visible_counts["archive"],
                total_count=archive_total if archive_enabled else None,
                truncated=archive_enabled
                and visible_counts["archive"] < archive_total,
                note=(
                    f"전체 actionable {archive_total}건 중 현재 page "
                    f"{visible_counts['archive']}건 표시"
                    if archive_enabled
                    else _SOURCE_DISABLED_NOTES["archive"]
                ),
            ),
            _source_payload(
                "timetable",
                ready=timetable_enabled,
                visible_count=visible_counts["timetable"],
                total_count=timetable_total if timetable_enabled else None,
                truncated=timetable_enabled
                and visible_counts["timetable"] < timetable_total,
                note=(
                    f"전체 suggested {timetable_total}건 중 현재 page "
                    f"{visible_counts['timetable']}건 표시"
                    if timetable_enabled
                    else _SOURCE_DISABLED_NOTES["timetable"]
                ),
            ),
            _source_payload(
                "title",
                ready=title_enabled,
                visible_count=visible_counts["title"],
                total_count=title_total if title_enabled else None,
                truncated=title_enabled
                and visible_counts["title"] < title_total,
                note=(
                    f"전체 suggested {title_total}건 중 현재 page "
                    f"{visible_counts['title']}건 표시"
                    if title_enabled
                    else _SOURCE_DISABLED_NOTES["title"]
                ),
            ),
            _source_payload(
                "recording",
                ready=recording_enabled,
                visible_count=visible_counts["recording"],
                total_count=recording_total if recording_enabled else None,
                truncated=recording_enabled
                and visible_counts["recording"] < recording_total,
                note=(
                    _recording_note(
                        recording_total=recording_total,
                        visible_count=visible_counts["recording"],
                        open_review_total=recording_open_review_total,
                        timetable_linked_review_total=(
                            recording_timetable_linked_review_total
                        ),
                        title_linked_review_total=(
                            recording_title_linked_review_total
                        ),
                        linked_review_total=recording_linked_review_total,
                        remaining_review_total=recording_remaining_review_total,
                    )
                    if recording_enabled
                    else _SOURCE_DISABLED_NOTES["recording"]
                ),
            ),
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "available": any(source["available"] for source in sources),
            "filters": {
                "limit": limit,
                "offset": offset,
            },
            "total": total,
            "items": items,
            "sources": sources,
        }
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
