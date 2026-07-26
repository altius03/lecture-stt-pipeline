from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.archive_review import (
    ArchiveReviewConflictError,
    ArchiveReviewNotFoundError,
    ArchiveReviewWriteDisabledError,
)
from lecture_stt.storage_v2.library import (
    RecordingLibraryDisabledError,
    RecordingLibraryNotFoundError,
)
from lecture_stt.storage_v2.title_suggestions import (
    TitleSuggestionConflictError,
    TitleSuggestionNotFoundError,
    TitleSuggestionWriteDisabledError,
)
from lecture_stt.storage_v2.timetable import (
    TimetableConflictError,
    TimetableNotFoundError,
    TimetableWriteDisabledError,
)
from lecture_stt.ui.web_panel_state import ControlState


_JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_CANONICAL_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def react_dist_dir(root: Path | None = None) -> Path:
    base_root = root or repo_root()
    return base_root / "frontend" / "web-panel" / "dist"


def render_react_placeholder(root: Path | None = None) -> str:
    frontend_root = (root or repo_root()) / "frontend" / "web-panel"
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lecture STT Web Panel</title>
  <style>
    body {
      margin: 0;
      background: #f6f6f3;
      color: #18181b;
      font-family: "Avenir Next", "Helvetica Neue", "Noto Sans KR", sans-serif;
    }
    main {
      max-width: 720px;
      margin: 48px auto;
      padding: 28px;
    }
    .panel {
      background: #ffffff;
      border: 1px solid #d4d4d8;
      border-radius: 8px;
      padding: 28px;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 30px;
      line-height: 1.15;
    }
    p {
      margin: 0 0 12px;
      line-height: 1.6;
    }
    code {
      background: #f4f4f5;
      border-radius: 6px;
      padding: 2px 7px;
    }
    .steps {
      margin-top: 20px;
      padding-left: 18px;
    }
    .steps li {
      margin-bottom: 8px;
    }
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1>웹 패널 빌드가 필요합니다.</h1>
      <p>메인 패널은 React 빌드 산출물 <code>frontend/web-panel/dist</code>를 사용합니다.</p>
      <ol class="steps">
        <li><code>cd __FRONTEND_ROOT__</code></li>
        <li><code>npm install</code></li>
        <li><code>npm run build</code></li>
        <li>브라우저에서 <code>http://127.0.0.1:8765</code> 접속</li>
      </ol>
    </section>
  </main>
</body>
</html>""".replace("__FRONTEND_ROOT__", html.escape(str(frontend_root)))


def _resolve_react_asset(request_path: str) -> Path | None:
    dist_root = react_dist_dir().resolve()
    if not dist_root.exists():
        return None

    relative_path = request_path.lstrip("/")
    if not relative_path:
        relative_path = "index.html"

    candidate = (dist_root / relative_path).resolve()
    try:
        candidate.relative_to(dist_root)
    except ValueError:
        return None

    if not candidate.is_file():
        return None
    return candidate


def _state_stream_signature(snapshot: dict) -> str:
    summary = snapshot.get("summary", {})
    stable_payload = {
        "runtime_state": snapshot.get("runtime_state"),
        "notification": snapshot.get("notification"),
        "counts": snapshot.get("counts"),
        "folders": snapshot.get("folders"),
        "actions": snapshot.get("actions"),
        "jobs_v2": snapshot.get("jobs_v2"),
        "processing_v2": snapshot.get("processing_v2"),
        "summary": {
            "current_job": summary.get("current_job"),
            "progress_label": summary.get("progress_label"),
            "eta_label": summary.get("eta_label"),
            "notice": summary.get("notice"),
            "refresh_hint": summary.get("refresh_hint"),
        },
    }
    return json.dumps(stable_payload, ensure_ascii=False, sort_keys=True)


def _parse_event_streams(query: dict[str, list[str]]) -> tuple[str, ...]:
    raw_values = query.get("streams")
    if raw_values is None:
        return ("state", "logs")
    if len(raw_values) != 1:
        raise ValueError(
            "streams must be a comma-separated list of state and/or logs"
        )

    raw = raw_values[0]
    selected: list[str] = []
    for token in raw.split(","):
        stream = token.strip()
        if (
            not stream
            or stream not in {"state", "logs"}
            or stream in selected
        ):
            raise ValueError(
                "streams must be a comma-separated list of state and/or logs"
            )
        selected.append(stream)

    if not selected:
        raise ValueError(
            "streams must be a comma-separated list of state and/or logs"
        )
    return tuple(selected)


class RequestHandler(BaseHTTPRequestHandler):
    JSON_BODY_LIMIT_BYTES = 32 * 1024
    _DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")

    def _read_form_fields(self) -> dict[str, str]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = max(0, int(raw_length))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _read_json_object(self) -> dict:
        content_type = str(self.headers.get("Content-Type", "")).split(
            ";",
            1,
        )[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0:
            raise ValueError("JSON request body is required")
        if length > self.JSON_BODY_LIMIT_BYTES:
            raise ValueError("JSON request body is too large")
        try:
            payload = json.loads(
                self.rfile.read(length).decode("utf-8", errors="strict")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object")
        return payload

    def _read_optional_json_object(self) -> dict:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0:
            return {}
        return self._read_json_object()

    protocol_version = "HTTP/1.1"

    def _write_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self._write_bytes(code, encoded, content_type)

    def _write_json(self, code: int, payload: dict) -> None:
        self._write(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _write_sse_event(self, name: str, payload: dict) -> None:
        body = f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()

    def _write_sse_comment(self, comment: str) -> None:
        self.wfile.write(f": {comment}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_file(self, path: Path) -> None:
        content_type, _ = mimetypes.guess_type(str(path))
        if path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        self._write_bytes(200, path.read_bytes(), content_type or "application/octet-stream")

    def _action_start(self) -> None:
        assert STATE is not None
        STATE.start()
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_pause(self) -> None:
        assert STATE is not None
        STATE.pause()
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_resume(self) -> None:
        assert STATE is not None
        STATE.resume()
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_stop(self) -> None:
        assert STATE is not None
        STATE.stop()
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_refresh(self) -> None:
        self._write_json(200, {"ok": True})

    def _action_clear_history(self) -> None:
        assert STATE is not None
        STATE.clear_history()
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_notification_update(self, selection: str, apply_now: bool = True) -> None:
        assert STATE is not None
        try:
            STATE.update_notification_selection(selection)
            if apply_now:
                STATE.restart_worker_for_notification()
        except RuntimeError as exc:
            self._api_error(400, str(exc))
            return
        self._write_json(200, {"ok": True, "notice": STATE.notice})

    def _action_exit(self) -> None:
        assert STATE is not None
        STATE.request_shutdown()
        self._write_json(200, {"ok": True, "notice": "웹 제어판 종료 요청됨"})

    def _redirect_root(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_error(self, status: int, message: str) -> None:
        self._write_json(status, {"ok": False, "error": message})

    def _archive_review_api_error(self, exc: Exception) -> None:
        if isinstance(exc, ArchiveReviewNotFoundError):
            self._api_error(404, str(exc))
            return
        if isinstance(exc, ArchiveReviewWriteDisabledError):
            self._api_error(403, str(exc))
            return
        if isinstance(exc, ArchiveReviewConflictError):
            self._api_error(409, str(exc))
            return
        if isinstance(exc, ValueError):
            self._api_error(400, str(exc))
            return
        self._api_error(503, str(exc))

    def _timetable_api_error(self, exc: Exception) -> None:
        if isinstance(exc, TimetableNotFoundError):
            self._api_error(404, str(exc))
            return
        if isinstance(exc, TimetableWriteDisabledError):
            self._api_error(403, str(exc))
            return
        if isinstance(exc, TimetableConflictError):
            self._api_error(409, str(exc))
            return
        if isinstance(exc, ValueError):
            self._api_error(400, str(exc))
            return
        self._api_error(503, str(exc))

    def _transcription_analytics_api_error(self, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            self._api_error(400, str(exc))
            return
        self._api_error(503, str(exc))

    def _recording_library_api_error(self, exc: Exception) -> None:
        if isinstance(exc, RecordingLibraryNotFoundError):
            self._api_error(404, str(exc))
            return
        if isinstance(exc, RecordingLibraryDisabledError):
            self._api_error(403, str(exc))
            return
        if isinstance(exc, ValueError):
            self._api_error(400, str(exc))
            return
        self._api_error(503, "Recording library is unavailable")

    def _title_review_api_error(self, exc: Exception) -> None:
        if isinstance(exc, TitleSuggestionNotFoundError):
            self._api_error(404, str(exc))
            return
        if isinstance(exc, TitleSuggestionWriteDisabledError):
            self._api_error(403, str(exc))
            return
        if isinstance(exc, TitleSuggestionConflictError):
            self._api_error(409, str(exc))
            return
        self._api_error(503, "Title review is unavailable")

    def _unified_review_api_error(self, exc: Exception) -> None:
        self._api_error(503, "Unified review feed is unavailable")

    @staticmethod
    def _archive_review_case_route(
        path: str,
    ) -> tuple[str, tuple[str, ...]] | None:
        prefix = "/api/storage-v2/archive-evidence/cases/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        parts = tuple(part for part in suffix.split("/") if part)
        if not parts:
            return None
        return parts[0], parts[1:]

    @staticmethod
    def _timetable_classification_route(
        path: str,
    ) -> tuple[int, tuple[str, ...]] | None:
        prefix = "/api/storage-v2/timetable/classifications/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        parts = tuple(part for part in suffix.split("/") if part)
        if not parts:
            return None
        try:
            proposal_id = int(parts[0])
        except ValueError:
            raise ValueError("proposal_id must be a positive integer")
        if proposal_id <= 0:
            raise ValueError("proposal_id must be a positive integer")
        return proposal_id, parts[1:]

    @staticmethod
    def _recording_library_route(path: str) -> tuple[str, tuple[str, ...]] | None:
        prefix = "/api/storage-v2/library/recordings/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        if not suffix:
            return None
        if "/" in suffix:
            head, _separator, tail = suffix.partition("/")
            return head, (tail or "/",)
        return suffix, ()

    @staticmethod
    def _title_review_proposal_route(
        path: str,
    ) -> tuple[int, tuple[str, ...]] | None:
        prefix = "/api/storage-v2/title-suggestions/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        if not suffix:
            return None
        parts = suffix.split("/")
        if any(part == "" for part in parts):
            raise ValueError(
                "proposal_id must be a canonical positive integer"
            )
        proposal_literal = parts[0]
        if not _CANONICAL_POSITIVE_INTEGER_RE.fullmatch(proposal_literal):
            raise ValueError(
                "proposal_id must be a canonical positive integer"
            )
        proposal_id = int(proposal_literal)
        if proposal_id > _JS_SAFE_INTEGER_MAX:
            raise ValueError("proposal_id exceeds the public integer limit")
        return proposal_id, tuple(parts[1:])

    @staticmethod
    def _require_exact_query_keys(
        query: dict[str, list[str]],
        *,
        allowed: tuple[str, ...],
    ) -> None:
        unknown = sorted(set(query).difference(allowed))
        if unknown:
            raise ValueError(
                "only the following query parameters are allowed: "
                + ", ".join(allowed)
            )

    @staticmethod
    def _require_exact_json_keys(
        payload: dict[str, object],
        *,
        allowed: tuple[str, ...],
    ) -> None:
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(
                "only the following JSON fields are allowed: "
                + ", ".join(allowed)
            )

    @classmethod
    def _single_query_int(
        cls,
        query: dict[str, list[str]],
        name: str,
        *,
        default: int,
    ) -> int:
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1:
            if name == "limit":
                raise ValueError("limit must be between 0 and 200")
            raise ValueError("offset must be between 0 and 100000")
        raw_value = values[0]
        if not raw_value or not cls._DECIMAL_RE.fullmatch(raw_value):
            if name == "limit":
                raise ValueError("limit must be between 0 and 200")
            raise ValueError("offset must be between 0 and 100000")
        try:
            return int(raw_value)
        except (TypeError, ValueError) as exc:
            if name == "limit":
                raise ValueError("limit must be between 0 and 200") from exc
            raise ValueError("offset must be between 0 and 100000") from exc

    @staticmethod
    def _single_query_text(
        query: dict[str, list[str]],
        name: str,
    ) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} must be a non-empty single value")
        return values[0]

    def _serve_archive_review_list(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("status", "limit", "offset"),
            )
            review_status = self._single_query_text(query, "status")
            limit = self._single_query_int(query, "limit", default=20)
            offset = self._single_query_int(query, "offset", default=0)
            payload = STATE.archive_review_list(
                review_status=review_status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._archive_review_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_archive_review_detail(self, case_key: str) -> None:
        assert STATE is not None
        try:
            payload = STATE.archive_review_detail(case_key)
        except Exception as exc:
            self._archive_review_api_error(exc)
            return
        self._write_json(200, payload)

    def _action_archive_review_status(self, case_key: str) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            review_status = payload.get("review_status")
            if not isinstance(review_status, str):
                raise ValueError("review_status must be a string")
            allow_write = payload.get("allow_write", False)
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
            result = STATE.archive_review_update_status(
                case_key,
                review_status,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._archive_review_api_error(exc)
            return
        self._write_json(200, result)

    @staticmethod
    def _promotion_request_fields(
        payload: dict,
    ) -> tuple[str, dict[str, int]]:
        target_storage_key = payload.get("target_storage_key")
        selected_revisions = payload.get("selected_revisions")
        if not isinstance(target_storage_key, str):
            raise ValueError("target_storage_key must be a string")
        if not isinstance(selected_revisions, dict):
            raise ValueError("selected_revisions must be an object")
        return target_storage_key, selected_revisions

    def _action_archive_review_promotion_plan(self, case_key: str) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            target_storage_key, selected_revisions = (
                self._promotion_request_fields(payload)
            )
            result = STATE.archive_review_plan_promotion(
                case_key,
                target_storage_key=target_storage_key,
                selected_revisions=selected_revisions,
            )
        except Exception as exc:
            self._archive_review_api_error(exc)
            return
        self._write_json(200, result)

    def _action_archive_review_promotion_apply(self, case_key: str) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            target_storage_key, selected_revisions = (
                self._promotion_request_fields(payload)
            )
            expected_count = payload.get("expected_count")
            expected_plan_sha256 = payload.get("expected_plan_sha256")
            allow_write = payload.get("allow_write", False)
            if isinstance(expected_count, bool) or not isinstance(
                expected_count,
                int,
            ):
                raise ValueError("expected_count must be an integer")
            if not isinstance(expected_plan_sha256, str):
                raise ValueError("expected_plan_sha256 must be a string")
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
            result = STATE.archive_review_apply_promotion(
                case_key,
                target_storage_key=target_storage_key,
                selected_revisions=selected_revisions,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._archive_review_api_error(exc)
            return
        self._write_json(200, result)

    def _serve_timetable_list(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("semester", "limit", "offset"),
            )
            semester = self._single_query_text(query, "semester")
            limit = self._single_query_int(query, "limit", default=100)
            offset = self._single_query_int(query, "offset", default=0)
            payload = STATE.timetable_list(
                semester=semester,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_timetable_classification_list(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("status", "limit", "offset"),
            )
            status = self._single_query_text(query, "status")
            limit = self._single_query_int(query, "limit", default=50)
            offset = self._single_query_int(query, "offset", default=0)
            payload = STATE.timetable_classification_list(
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_transcription_analytics(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        raw_periods = query.get("period")
        if raw_periods is None:
            period = "week"
        else:
            if len(raw_periods) != 1:
                self._api_error(400, "period must be one of: day, week, month")
                return
            period = raw_periods[0]
        try:
            payload = STATE.transcription_analytics(period=period)
        except Exception as exc:
            self._transcription_analytics_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_transcription_analytics_prometheus(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(query, allowed=("period",))
            raw_periods = query.get("period")
            if raw_periods is None:
                period = "week"
            else:
                if len(raw_periods) != 1:
                    raise ValueError(
                        "period must be one of: day, week, month"
                    )
                period = raw_periods[0]
            payload = STATE.transcription_analytics_prometheus(period=period)
        except Exception as exc:
            self._transcription_analytics_api_error(exc)
            return
        self._write(
            200,
            payload,
            "text/plain; version=0.0.4; charset=utf-8",
        )

    def _serve_recording_library_list(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("limit", "offset"),
            )
            limit = self._single_query_int(query, "limit", default=50)
            offset = self._single_query_int(query, "offset", default=0)
            payload = STATE.recording_library_list(
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._recording_library_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_unified_review_feed(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("limit", "offset"),
            )
            limit = self._single_query_int(query, "limit", default=50)
            offset = self._single_query_int(query, "offset", default=0)
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        try:
            payload = STATE.unified_review_feed(
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._unified_review_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_title_review_list(
        self,
        query: dict[str, list[str]],
    ) -> None:
        assert STATE is not None
        try:
            self._require_exact_query_keys(
                query,
                allowed=("status", "limit", "offset"),
            )
            raw_status = query.get("status")
            if raw_status is None:
                status = None
            else:
                if len(raw_status) != 1:
                    raise ValueError(
                        "status must be suggested, confirmed, or rejected"
                    )
                status = raw_status[0]
                if status not in {"suggested", "confirmed", "rejected"}:
                    raise ValueError(
                        "status must be suggested, confirmed, or rejected"
                    )
            limit = self._single_query_int(query, "limit", default=50)
            offset = self._single_query_int(query, "offset", default=0)
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        try:
            payload = STATE.title_review_list(
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            self._title_review_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_title_review_detail(self, proposal_id: int) -> None:
        assert STATE is not None
        try:
            payload = STATE.title_review_detail(proposal_id)
        except Exception as exc:
            self._title_review_api_error(exc)
            return
        self._write_json(200, payload)

    def _action_title_review_status(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            self._require_exact_json_keys(
                payload,
                allowed=("status", "allow_write"),
            )
            status = payload.get("status")
            allow_write = payload.get("allow_write", False)
            if not isinstance(status, str):
                raise ValueError("status must be a string")
            if status != "rejected":
                raise ValueError("status must be rejected")
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        try:
            result = STATE.title_review_status_update(
                proposal_id,
                status=status,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._title_review_api_error(exc)
            return
        self._write_json(200, result)

    def _action_title_review_confirmation_plan(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = self._read_optional_json_object()
            if payload:
                raise ValueError(
                    "title suggestion confirmation plan does not accept request fields"
                )
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        try:
            result = STATE.title_review_confirmation_plan(proposal_id)
        except Exception as exc:
            self._title_review_api_error(exc)
            return
        self._write_json(200, result)

    def _action_title_review_confirmation_apply(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            self._require_exact_json_keys(
                payload,
                allowed=(
                    "expected_count",
                    "expected_plan_sha256",
                    "allow_write",
                ),
            )
            expected_count = payload.get("expected_count")
            expected_plan_sha256 = payload.get("expected_plan_sha256")
            allow_write = payload.get("allow_write", False)
            if isinstance(expected_count, bool) or not isinstance(
                expected_count,
                int,
            ):
                raise ValueError("expected_count must be an integer")
            if expected_count != 1:
                raise ValueError("expected_count must equal 1")
            if not isinstance(expected_plan_sha256, str):
                raise ValueError("expected_plan_sha256 must be a string")
            if not _SHA256_RE.fullmatch(expected_plan_sha256):
                raise ValueError(
                    "expected_plan_sha256 must be a canonical SHA-256"
                )
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        try:
            result = STATE.title_review_confirmation_apply(
                proposal_id,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._title_review_api_error(exc)
            return
        self._write_json(200, result)

    def _serve_recording_library_detail(self, storage_key: str) -> None:
        assert STATE is not None
        try:
            payload = STATE.recording_library_detail(storage_key)
        except Exception as exc:
            self._recording_library_api_error(exc)
            return
        self._write_json(200, payload)

    def _serve_timetable_classification_detail(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = STATE.timetable_classification_detail(proposal_id)
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, payload)

    def _action_timetable_confirmation_plan(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            self._read_optional_json_object()
            result = STATE.timetable_confirmation_plan(proposal_id)
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, result)

    def _action_timetable_classification_status(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            status = payload.get("status")
            allow_write = payload.get("allow_write", False)
            if not isinstance(status, str):
                raise ValueError("status must be a string")
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
            result = STATE.timetable_classification_status_update(
                proposal_id,
                status=status,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, result)

    def _action_timetable_confirmation_apply(
        self,
        proposal_id: int,
    ) -> None:
        assert STATE is not None
        try:
            payload = self._read_json_object()
            expected_count = payload.get("expected_count")
            expected_plan_sha256 = payload.get("expected_plan_sha256")
            allow_write = payload.get("allow_write", False)
            if isinstance(expected_count, bool) or not isinstance(
                expected_count,
                int,
            ):
                raise ValueError("expected_count must be an integer")
            if not isinstance(expected_plan_sha256, str):
                raise ValueError("expected_plan_sha256 must be a string")
            if not isinstance(allow_write, bool):
                raise ValueError("allow_write must be a boolean")
            result = STATE.timetable_confirmation_apply(
                proposal_id,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
                allow_write=allow_write,
            )
        except Exception as exc:
            self._timetable_api_error(exc)
            return
        self._write_json(200, result)

    def _serve_event_stream(
        self,
        *,
        include_state: bool = True,
        include_logs: bool = True,
    ) -> None:
        assert STATE is not None

        self.send_response(200)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_state_signature: str | None = None
        log_offset: int | None = None
        last_heartbeat = time.monotonic()

        try:
            while True:
                snapshot = STATE.snapshot(include_log=False)
                signature = _state_stream_signature(snapshot)
                sent_event = False

                if include_state and signature != last_state_signature:
                    self._write_sse_event("state", snapshot)
                    last_state_signature = signature
                    sent_event = True

                if include_logs:
                    next_log_offset, log_text, reset = STATE._log_stream_delta(
                        log_offset
                    )
                    if reset:
                        self._write_sse_event(
                            "log_reset",
                            {"offset": next_log_offset, "text": log_text},
                        )
                        sent_event = True
                    elif log_text:
                        self._write_sse_event(
                            "log_chunk",
                            {"offset": next_log_offset, "text": log_text},
                        )
                        sent_event = True
                    log_offset = next_log_offset

                now = time.monotonic()
                if not sent_event and now - last_heartbeat >= 15.0:
                    self._write_sse_comment("heartbeat")
                    last_heartbeat = now

                poll_interval = snapshot.get("summary", {}).get("poll_interval_sec", 1.0)
                try:
                    sleep_sec = max(0.5, float(poll_interval))
                except (TypeError, ValueError):
                    sleep_sec = 1.0
                time.sleep(sleep_sec)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/storage-v2/analytics/transcriptions":
            self._serve_transcription_analytics(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return
        if parsed.path == "/api/storage-v2/analytics/transcriptions/metrics":
            self._serve_transcription_analytics_prometheus(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        if parsed.path == "/api/storage-v2/library/recordings":
            self._serve_recording_library_list(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        if parsed.path == "/api/storage-v2/review-feed":
            self._serve_unified_review_feed(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        if parsed.path == "/api/storage-v2/title-suggestions":
            self._serve_title_review_list(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        if parsed.path == "/api/state":
            assert STATE is not None
            self._write_json(200, STATE.snapshot())
            return

        if parsed.path == "/api/logs":
            assert STATE is not None
            offset_raw = query.get("offset", [None])[0]
            try:
                offset = int(offset_raw) if offset_raw is not None else None
            except ValueError:
                offset = None
            log_offset, text_data = STATE._log_delta(offset)
            self._write_json(200, {"offset": log_offset, "text": text_data})
            return

        if parsed.path == "/api/events":
            try:
                streams = _parse_event_streams(
                    parse_qs(parsed.query, keep_blank_values=True)
                )
            except ValueError as exc:
                self._api_error(400, str(exc))
                return
            self._serve_event_stream(
                include_state="state" in streams,
                include_logs="logs" in streams,
            )
            return

        if parsed.path == "/api/storage-v2/archive-evidence/cases":
            self._serve_archive_review_list(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        recording_library_route = self._recording_library_route(parsed.path)
        if recording_library_route is not None:
            storage_key, action_parts = recording_library_route
            if parsed.query:
                self._api_error(
                    400,
                    "recording detail does not accept query parameters",
                )
                return
            if not action_parts:
                self._serve_recording_library_detail(storage_key)
                return
            self._api_error(404, "Not Found")
            return

        try:
            title_review_route = self._title_review_proposal_route(
                parsed.path
            )
        except ValueError as exc:
            self._api_error(400, str(exc))
            return
        if title_review_route is not None:
            proposal_id, action_parts = title_review_route
            if parsed.query:
                self._api_error(
                    400,
                    "title suggestion detail does not accept query parameters",
                )
                return
            if not action_parts:
                self._serve_title_review_detail(proposal_id)
                return
            self._api_error(404, "Not Found")
            return

        archive_review_route = self._archive_review_case_route(parsed.path)
        if archive_review_route is not None:
            case_key, action_parts = archive_review_route
            if not action_parts:
                self._serve_archive_review_detail(case_key)
                return
            self._api_error(404, "Not Found")
            return

        if parsed.path == "/api/storage-v2/timetable/entries":
            self._serve_timetable_list(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        if parsed.path == "/api/storage-v2/timetable/classifications":
            self._serve_timetable_classification_list(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return

        try:
            timetable_route = self._timetable_classification_route(parsed.path)
        except ValueError as exc:
            self._timetable_api_error(exc)
            return
        if timetable_route is not None:
            proposal_id, action_parts = timetable_route
            if not action_parts:
                self._serve_timetable_classification_detail(proposal_id)
                return
            self._api_error(404, "Not Found")
            return

        if parsed.path.startswith("/api/"):
            self._api_error(404, "Not Found")
            return

        if parsed.path == "/app" or parsed.path.startswith("/app/"):
            self._redirect_root()
            return

        asset = _resolve_react_asset(parsed.path)
        if asset is not None:
            self._write_file(asset)
            return

        if parsed.path == "/":
            self._write(200, render_react_placeholder())
            return

        if Path(parsed.path).suffix:
            self._write(404, "Not Found", "text/plain; charset=utf-8")
            return

        index_asset = _resolve_react_asset("/index.html")
        if index_asset is not None:
            self._write_file(index_asset)
            return

        self._write(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/"):
            archive_review_route = self._archive_review_case_route(parsed.path)
            if archive_review_route is not None:
                case_key, action_parts = archive_review_route
                if action_parts == ("status",):
                    self._action_archive_review_status(case_key)
                    return
                if action_parts == ("promotion", "plan"):
                    self._action_archive_review_promotion_plan(case_key)
                    return
                if action_parts == ("promotion", "apply"):
                    self._action_archive_review_promotion_apply(case_key)
                    return
                self._api_error(404, "Not Found")
                return
            try:
                timetable_route = self._timetable_classification_route(
                    parsed.path
                )
            except ValueError as exc:
                self._timetable_api_error(exc)
                return
            if timetable_route is not None:
                proposal_id, action_parts = timetable_route
                if action_parts == ("status",):
                    self._action_timetable_classification_status(proposal_id)
                    return
                if action_parts == ("confirmation", "plan"):
                    self._action_timetable_confirmation_plan(proposal_id)
                    return
                if action_parts == ("confirmation", "apply"):
                    self._action_timetable_confirmation_apply(proposal_id)
                    return
                self._api_error(404, "Not Found")
                return
            try:
                title_review_route = self._title_review_proposal_route(
                    parsed.path
                )
            except ValueError as exc:
                self._api_error(400, str(exc))
                return
            if title_review_route is not None:
                proposal_id, action_parts = title_review_route
                if parsed.query:
                    self._api_error(
                        400,
                        "title suggestion actions do not accept query parameters",
                    )
                    return
                if action_parts == ("status",):
                    self._action_title_review_status(proposal_id)
                    return
                if action_parts == ("confirmation", "plan"):
                    self._action_title_review_confirmation_plan(proposal_id)
                    return
                if action_parts == ("confirmation", "apply"):
                    self._action_title_review_confirmation_apply(
                        proposal_id
                    )
                    return
                self._api_error(404, "Not Found")
                return
            if parsed.path == "/api/start":
                self._action_start()
                return
            if parsed.path == "/api/pause":
                self._action_pause()
                return
            if parsed.path == "/api/resume":
                self._action_resume()
                return
            if parsed.path == "/api/stop":
                self._action_stop()
                return
            if parsed.path == "/api/refresh":
                self._action_refresh()
                return
            if parsed.path == "/api/exit":
                self._action_exit()
                return
            if parsed.path == "/api/clear_history":
                self._action_clear_history()
                return
            if parsed.path == "/api/notification":
                fields = self._read_form_fields()
                selection = fields.get("selection", "")
                apply_now_raw = fields.get("apply_now")
                if apply_now_raw is None or apply_now_raw == "":
                    apply_now = False
                else:
                    apply_now = apply_now_raw.strip().lower() in {"1", "true", "yes", "on"}
                self._action_notification_update(selection, apply_now=apply_now)
                return
            self._api_error(404, "Not Found")
            return

        self._write(404, "Not Found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


STATE: ControlState | None = None


def main() -> None:
    root = repo_root()
    host = os.environ.get("WEB_PANEL_HOST", "127.0.0.1")
    port_env = os.environ.get("WEB_PANEL_PORT", "8765")
    try:
        port = int(port_env)
    except ValueError as exc:
        raise RuntimeError(f"Invalid WEB_PANEL_PORT: {port_env}") from exc

    global STATE
    STATE = ControlState(repo_root=root)

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    try:
        server = ThreadingHTTPServer((host, port), RequestHandler)
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot bind web control panel to {host}:{port}. Permission denied: {exc}. "
            "Check sandbox/security policy or use a non-restricted shell/session."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot bind web control panel to {host}:{port}. {exc}") from exc

    STATE.set_shutdown_handler(server.shutdown)
    print(f"Web control panel running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
