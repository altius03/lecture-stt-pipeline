from __future__ import annotations

import html
import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lecture_stt.shared.paths import repo_root
from lecture_stt.ui.web_panel_state import ControlState


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


class RequestHandler(BaseHTTPRequestHandler):
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

    def _serve_event_stream(self) -> None:
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

                if signature != last_state_signature:
                    self._write_sse_event("state", snapshot)
                    last_state_signature = signature
                    sent_event = True

                next_log_offset, log_text, reset = STATE._log_stream_delta(log_offset)
                if reset:
                    self._write_sse_event("log_reset", {"offset": next_log_offset, "text": log_text})
                    sent_event = True
                elif log_text:
                    self._write_sse_event("log_chunk", {"offset": next_log_offset, "text": log_text})
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
            self._serve_event_stream()
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
