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
  <title>Lecture STT React Panel</title>
  <style>
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(196, 224, 255, 0.8), transparent 34%),
        linear-gradient(180deg, #f5f4ed 0%, #ebe8dc 100%);
      color: #17313e;
      font-family: "Avenir Next", "Helvetica Neue", "Noto Sans KR", sans-serif;
    }
    main {
      max-width: 780px;
      margin: 48px auto;
      padding: 28px;
    }
    .panel {
      background: rgba(255, 252, 245, 0.92);
      border: 1px solid rgba(23, 49, 62, 0.12);
      border-radius: 24px;
      box-shadow: 0 24px 60px rgba(23, 49, 62, 0.10);
      padding: 28px;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 34px;
      line-height: 1.05;
      letter-spacing: -0.03em;
    }
    p {
      margin: 0 0 12px;
      line-height: 1.6;
    }
    code {
      background: rgba(23, 49, 62, 0.06);
      border-radius: 8px;
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
      <h1>React 패널 빌드가 아직 없습니다.</h1>
      <p>React 웹 패널 골격은 저장소에 추가됐지만, 현재는 빌드 산출물 <code>frontend/web-panel/dist</code>가 없어서 레거시 패널만 바로 열립니다.</p>
      <p>지금은 기존 패널을 <code>/</code>에서 계속 사용할 수 있고, React 패널은 빌드 후 <code>/app</code>에서 열립니다.</p>
      <ol class="steps">
        <li><code>cd __FRONTEND_ROOT__</code></li>
        <li><code>npm install</code></li>
        <li><code>npm run build</code></li>
        <li>브라우저에서 <code>http://127.0.0.1:8765/app</code> 접속</li>
      </ol>
    </section>
  </main>
</body>
</html>""".replace("__FRONTEND_ROOT__", html.escape(str(frontend_root)))


def _resolve_react_asset(request_path: str) -> Path | None:
    dist_root = react_dist_dir().resolve()
    if not dist_root.exists():
        return None

    relative_path = request_path.removeprefix("/app").lstrip("/")
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


def render_html(snapshot: dict) -> str:
    jobs_rows: list[str] = []
    for row in snapshot["jobs"]:
        job_id, status, orig_name, updated_at, step, progress, error_msg = row
        jobs_rows.append(
            "<tr>"
            "<td>" + html.escape(str(job_id)) + "</td>"
            "<td>" + html.escape(str(status)) + "</td>"
            "<td>" + html.escape(str(orig_name or "")) + "</td>"
            "<td>" + html.escape(str(updated_at or "")) + "</td>"
            "<td>" + html.escape(str(progress)) + "%</td>"
            "<td>" + html.escape(str(step or "")) + "</td>"
            "<td>" + html.escape(str(error_msg or "")) + "</td>"
            "</tr>"
        )
    jobs_html = "\n".join(jobs_rows) or "<tr><td colspan='7'>작업 없음</td></tr>"

    processing_rows: list[str] = []
    for job_id, file_name, step, progress, eta_sec in snapshot["processing"]:
        try:
            eta_value = int(eta_sec) if eta_sec else 0
        except Exception:
            eta_value = 0

        if eta_value:
            from datetime import datetime as _dt, timedelta as _td
            finish = _dt.now() + _td(seconds=eta_value)
            eta_text = finish.strftime("%H:%M:%S")
        else:
            eta_text = "-"
        processing_rows.append(
            "<li>" +
            "#" + html.escape(str(job_id)) + " " + html.escape(str(file_name)) + " : "
            + html.escape(str(step or "파일 감지")) + " ("
            + html.escape(str(progress)) + "%) / 이 파일 완료예정 " + html.escape(eta_text) +
            "</li>"
        )
    processing_html = "\n".join(processing_rows) or "<li>현재 처리중인 작업 없음</li>"

    style = """
    body { font-family: Arial, Helvetica, sans-serif; margin: 16px; }
    h1 { margin: 0 0 8px 0; font-size: 26px; }
    .row { display: flex; gap: 12px; margin-bottom: 12px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 10px; flex: 1; }
    .card h3 { margin-top: 0; }
    button { padding: 8px 12px; font-size: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border: 1px solid #ddd; padding: 6px; text-align: left; }
    th { background: #f7f7f7; }
    .meta { color: #333; font-size: 13px; margin: 2px 0; }
    .meta-note { color: #555; font-size: 12px; }
    .guide { background: #f8f8f8; border: 1px solid #ececec; }
    .guide ul { margin: 4px 0 0 18px; padding: 0; }
    .guide li { margin-bottom: 6px; font-size: 12px; color: #333; line-height: 1.35; }
    .notice { color: #0b62a4; }
    .log { background: #111; color: #ddd; padding: 10px; border-radius: 8px; overflow: auto; height: 280px; white-space: pre-wrap; }
    .small { font-size: 12px; color: #666; }
    .controls form { display: inline-block; margin-right: 8px; margin-bottom: 6px; }
    ul { padding-left: 20px; margin: 0; }
    """

    template = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>강의 녹음 STT 웹 제어판</title>
  <style>@@STYLE@@</style>
</head>
<body>
  <h1>강의 녹음 STT 웹 제어판</h1>

  <div class=\"meta\">업데이트: <span id=\"updatedAt\">@@UPDATED_AT@@</span></div>
  <div class=\"meta\">상태: <strong id=\"runtime\">@@RUNTIME@@</strong> / <span id=\"runtimeDesc\">@@RUNTIME_DESC@@</span></div>
  <div class=\"meta\">실행원천: <span id=\"origin\">@@ORIGIN@@</span></div>
  <div class=\"meta\">현재 단계: <strong id=\"progressLabel\">@@PROGRESS@@</strong> / <span id=\"etaLabel\">@@ETA@@</span> </div>
  <div class=\"meta-note\">상태 동기화 버튼: 상태/이력은 즉시 새로고침, 로그는 변경분만 이어붙입니다.</div>
  <div class=\"meta\">현재 처리 작업</div>
  <div class=\"card\"><ul id=\"processingList\">@@PROCESSING@@</ul></div>
  <div class=\"notice\" id=\"notice\">@@NOTICE@@</div>
  <div class=\"small\" id=\"refreshHint\">@@REFRESH_HINT@@</div>

  <div class=\"row controls\">
    <div class=\"card\">
      <form method=\"post\" action=\"/action/start\" onsubmit=\"sendAction('start'); return false;\"><button type=\"submit\">시작</button></form>
      <form method=\"post\" action=\"/action/pause\" onsubmit=\"sendAction('@@PAUSE_LABEL@@'); return false;\"><button type=\"submit\" id=\"pauseBtn\">@@PAUSE_LABEL@@</button></form>
      <form method=\"post\" action=\"/action/stop\" onsubmit=\"sendAction('stop'); return false;\"><button type=\"submit\">중지</button></form>
      <form method=\"post\" action=\"/action/refresh\" onsubmit=\"sendAction('refresh'); return false;\"><button type=\"submit\">상태 동기화</button></form>
      <form method=\"post\" action=\"/action/exit\" onsubmit=\"sendAction('exit'); return false;\"><button type=\"submit\">종료</button></form>
      <form method=\"post\" action=\"/action/clear_history\" onsubmit=\"sendAction('clear_history'); return false;\"><button type=\"submit\">이력 초기화</button></form>
      <div class=\"small\">종료: 웹 제어판 서버만 종료됩니다. / 이력 초기화: 완료·오류 이력과 로그를 삭제합니다.</div>
      <label><input type=\"checkbox\" id=\"autoScroll\" checked /> 자동 스크롤</label>
    </div>
    <div class=\"card guide\">
      <h3>웹 제어판 사용 안내</h3>
      <ul>
        <li><b>현재 상태</b>: <span class=\"small\">실행중/일시정지/중지 상태와 원천(웹 시작 또는 외부 실행)을 확인합니다.</span></li>
        <li><b>시작</b>: 워커를 실행(또는 재시작)합니다.</li>
        <li><b>재개</b>: 일시정지된 상태에서 계속 진행합니다.</li>
        <li><b>중지</b>: 실행 중인 워커를 종료하고 즉시 대기 상태로 전환합니다.</li>
        <li><b>로그</b>: 상단 영역은 실시간 로그입니다. 새 로그가 아래로 이어집니다.</li>
        <li><b>이력</b>: 최근 작업 표에서 상태/오류/완료 단계와 파일명을 확인합니다.</li>
        <li><b>자동 스크롤</b>: 켜면 새 로그가 도착할 때 항상 아래로 이동합니다.</li>
      </ul>
    </div>
  </div>

  <div class=\"row\">
    <div class=\"card\">
      <h3>작업 상태 개수</h3>
      <div>대기(DB): <span id=\"countPending\">@@PENDING@@</span></div>
      <div>미등록(inbox): <span id=\"countUnregistered\">@@UNREGISTERED@@</span></div>
      <div>처리중: <span id=\"countProcessing\">@@PROCESSING_COUNT@@</span></div>
      <div>완료: <span id=\"countDone\">@@DONE@@</span></div>
      <div>오류: <span id=\"countError\">@@ERROR@@</span></div>
    </div>
    <div class=\"card\">
      <h3>폴더 현황</h3>
      <div>00_inbox: <span id=\"folderInbox\">@@FOLDER_INBOX@@</span></div>
      <div>01_audio: <span id=\"folderAudio\">@@FOLDER_AUDIO@@</span></div>
      <div>02_transcripts: <span id=\"folderTranscripts\">@@FOLDER_TRANSCRIPTS@@</span> 건 (txt+json=1쌍)</div>
      <div>99_errors: <span id=\"folderErrors\">@@FOLDER_ERRORS@@</span></div>
    </div>
  </div>

  <div class=\"card\">
    <h3>최근 작업</h3>
    <div class=\"small\">최근 작업은 ID/상태/파일명/최종수정/진행률/현재 단계/오류 입니다.</div>
    <table>
      <thead><tr><th>ID</th><th>상태</th><th>파일</th><th>업데이트</th><th>진행률</th><th>단계</th><th>오류</th></tr></thead>
      <tbody id=\"jobRows\">@@JOBS@@</tbody>
    </table>
  </div>

  <div class=\"card\">
    <h3>실행 로그 미리보기</h3>
    <div class=\"log\" id=\"logBox\">@@LOG@@</div>
  </div>

<script>
let logOffset = null;
let isManualScroll = false;
let tickTimer = null;
let nextPollMs = 1000;
let pollBoostUntil = 0;

function text(v) {
  return (v === null || v === undefined) ? '' : String(v);
}

function setActionLabel(label) {
  const button = document.getElementById('pauseBtn');
  if (button) {
    button.textContent = label;
  }
}

function formatProgressValue(value) {
  if (value === null || value === undefined || value === '') {
    return '-';
  }
  return text(value);
}

function trimLogLines(element) {
  const maxLines = 500;
  const lines = element.textContent.split('\n');
  if (lines.length <= maxLines) {
    return;
  }
  element.textContent = lines.slice(lines.length - maxLines).join('\n');
}

function isLogAtBottom(element) {
  return (element.scrollTop + element.clientHeight) >= (element.scrollHeight - 6);
}

function formatEta(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) {
    return '-';
  }
  const finish = new Date(Date.now() + value * 1000);
  const hh = String(finish.getHours()).padStart(2, '0');
  const mm = String(finish.getMinutes()).padStart(2, '0');
  const ss = String(finish.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function updateJobs(rows) {
  const target = document.getElementById('jobRows');
  let html = '';
  if (!rows || rows.length === 0) {
    target.innerHTML = '<tr><td colspan="7">작업 없음</td></tr>';
    return;
  }

  for (const row of rows) {
    const [job_id, status, orig_name, updated_at, step, progress, error_msg] = row;
    html += '<tr>' +
      '<td>' + text(job_id) + '</td>' +
      '<td>' + text(status) + '</td>' +
      '<td>' + text(orig_name) + '</td>' +
      '<td>' + text(updated_at) + '</td>' +
      '<td>' + formatProgressValue(progress) + '%</td>' +
      '<td>' + text(step) + '</td>' +
      '<td>' + text(error_msg) + '</td>' +
      '</tr>';
  }
  target.innerHTML = html;
}

function updateProcessing(rows) {
  const target = document.getElementById('processingList');
  if (!rows || rows.length === 0) {
    target.innerHTML = '<li>현재 처리중인 작업 없음</li>';
    return;
  }

  let html = '';
  for (const row of rows) {
    const [job_id, file_name, step, progress, eta_sec] = row;
    const etaText = formatEta(eta_sec);
    html += `<li>#${text(job_id)} ${text(file_name)} : ${text(step)} (${text(progress)}%) / 이 파일 완료예정 ${etaText}</li>`;
  }
  target.innerHTML = html;
}

function applyState(snapshot) {
  document.getElementById('updatedAt').textContent = text(snapshot.updated_at);
  document.getElementById('runtime').textContent = text(snapshot.runtime);
  document.getElementById('runtimeDesc').textContent = text(snapshot.runtime_desc);
  document.getElementById('origin').textContent = text(snapshot.origin);
  document.getElementById('progressLabel').textContent = text(snapshot.progress);
  document.getElementById('etaLabel').textContent = text(snapshot.eta_text);
  document.getElementById('notice').textContent = text(snapshot.notice);
  document.getElementById('countPending').textContent = text(snapshot.counts.PENDING);
  document.getElementById('countUnregistered').textContent = text(snapshot.counts.UNREGISTERED);
  document.getElementById('countProcessing').textContent = text(snapshot.counts.PROCESSING);
  document.getElementById('countDone').textContent = text(snapshot.counts.DONE);
  document.getElementById('countError').textContent = text(snapshot.counts.ERROR);
  document.getElementById('folderInbox').textContent = text(snapshot.folders.inbox);
  document.getElementById('folderAudio').textContent = text(snapshot.folders.audio);
  document.getElementById('folderTranscripts').textContent = text(snapshot.folders.transcripts);
  document.getElementById('folderErrors').textContent = text(snapshot.folders.errors);
  document.getElementById('refreshHint').textContent = text(snapshot.refresh_hint);

  setActionLabel(text(snapshot.pause_button));
  updateJobs(snapshot.jobs || []);
  updateProcessing(snapshot.processing || []);
}

async function refreshState() {
  const response = await fetch('/api/state', { cache: 'no-store' });
  if (!response.ok) {
    return null;
  }
  const snapshot = await response.json();
  applyState(snapshot);
  return snapshot;
}

async function refreshLogs(force) {
  const query = logOffset === null ? '' : `?offset=${encodeURIComponent(logOffset)}`;
  const response = await fetch(`/api/logs${query}`, { cache: 'no-store' });
  if (!response.ok) {
    return;
  }

  const payload = await response.json();
  const payloadText = payload.text || '';
  const autoScroll = document.getElementById('autoScroll');
  const logBox = document.getElementById('logBox');

  if (payloadText) {
    const wasBottom = isLogAtBottom(logBox);
    logBox.textContent += payloadText;
    trimLogLines(logBox);
    if ((autoScroll && autoScroll.checked && (force || wasBottom)) || force) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  }

  logOffset = payload.offset;
  if (autoScroll && autoScroll.checked) {
    isManualScroll = false;
  }
}

function trackScrollState() {
  const logBox = document.getElementById('logBox');
  const autoScroll = document.getElementById('autoScroll');

  logBox.addEventListener('scroll', () => {
    const atBottom = isLogAtBottom(logBox);
    if (!atBottom) {
      isManualScroll = true;
      if (autoScroll) {
        autoScroll.checked = false;
      }
      return;
    }

    isManualScroll = false;
    if (autoScroll) {
      autoScroll.checked = true;
    }
  });

  const checkbox = document.getElementById('autoScroll');
  checkbox.addEventListener('change', () => {
    if (checkbox.checked && !isManualScroll) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  });
}

async function sendAction(name) {
  const knownActions = ['start', 'stop', 'exit', 'refresh', 'clear_history'];
  let endpoint;
  if (name === '재개') {
    endpoint = '/action/resume';
  } else if (name === '일시정지') {
    endpoint = '/action/pause';
  } else if (knownActions.includes(name)) {
    endpoint = '/action/' + name;
  } else {
    endpoint = '/api/' + name;
  }

  await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: ''
  });
  pollBoostUntil = Date.now() + 6000;
  await refreshState();
  await refreshLogs(true);
}

function scheduleNextPoll() {
  if (tickTimer) {
    clearTimeout(tickTimer);
    tickTimer = null;
  }

  const effectiveInterval = pollBoostUntil > Date.now() ? 500 : nextPollMs;
  tickTimer = setTimeout(() => {
    tickTimer = null;
    runTick();
  }, effectiveInterval);
}

async function runTick() {
  const snapshot = await refreshState();
  const forceLog = !!(snapshot && snapshot.poll_interval_sec === 0.5 && pollBoostUntil > Date.now());
  await refreshLogs(forceLog);

  if (snapshot && Number.isFinite(Number(snapshot.poll_interval_sec)) && Number(snapshot.poll_interval_sec) > 0) {
    nextPollMs = Math.max(200, Math.round(Number(snapshot.poll_interval_sec) * 1000));
  }
  if (snapshot && snapshot.runtime_desc) {
    nextPollMs = Math.max(200, Math.round((snapshot.poll_interval_sec || 1) * 1000));
  }
  scheduleNextPoll();
}

window.onload = () => {
  trackScrollState();
  refreshState();
  refreshLogs(true);
  runTick();
};
</script>
</body>
</html>"""

    return (
        template.replace("@@STYLE@@", style)
        .replace("@@UPDATED_AT@@", html.escape(snapshot["updated_at"]))
        .replace("@@RUNTIME@@", html.escape(snapshot["runtime"]))
        .replace("@@RUNTIME_DESC@@", html.escape(snapshot["runtime_desc"]))
        .replace("@@ORIGIN@@", html.escape(snapshot["origin"]))
        .replace("@@PROGRESS@@", html.escape(snapshot["progress"]))
        .replace("@@ETA@@", html.escape(snapshot["eta_text"]))
        .replace("@@PROCESSING@@", processing_html)
        .replace("@@NOTICE@@", html.escape(snapshot["notice"]))
        .replace("@@REFRESH_HINT@@", html.escape(snapshot["refresh_hint"]))
        .replace("@@PAUSE_LABEL@@", html.escape(snapshot["pause_button"]))
        .replace("@@PENDING@@", str(snapshot["counts"]["PENDING"]))
        .replace("@@UNREGISTERED@@", str(snapshot["counts"]["UNREGISTERED"]))
        .replace("@@PROCESSING_COUNT@@", str(snapshot["counts"]["PROCESSING"]))
        .replace("@@DONE@@", str(snapshot["counts"]["DONE"]))
        .replace("@@ERROR@@", str(snapshot["counts"]["ERROR"]))
        .replace("@@FOLDER_INBOX@@", html.escape(snapshot["folders"]["inbox"]))
        .replace("@@FOLDER_AUDIO@@", html.escape(snapshot["folders"]["audio"]))
        .replace("@@FOLDER_TRANSCRIPTS@@", html.escape(snapshot["folders"]["transcripts"]))
        .replace("@@FOLDER_ERRORS@@", html.escape(snapshot["folders"]["errors"]))
        .replace("@@JOBS@@", jobs_html)
        .replace("@@LOG@@", html.escape(snapshot["log_tail"]))
        .replace("@@POLL_INTERVAL@@", str(snapshot["poll_interval_sec"]))
    )


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

    def _action_start(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.start()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_pause(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.pause()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_resume(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.resume()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_stop(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.stop()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_refresh(self, api_mode: bool = False) -> None:
        if api_mode:
            self._write_json(200, {"ok": True})
        else:
            self._redirect_home()

    def _action_clear_history(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.clear_history()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_notification_update(self, selection: str, apply_now: bool = True, api_mode: bool = False) -> None:
        assert STATE is not None
        try:
            STATE.update_notification_selection(selection)
            if apply_now:
                STATE.restart_worker_for_notification()
        except RuntimeError as exc:
            if api_mode:
                self._api_error(400, str(exc))
            else:
                self._write(400, str(exc), "text/plain; charset=utf-8")
            return
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_exit(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.request_shutdown()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": "웹 제어판 종료 요청됨"})
        else:
            self._write(303, "웹 제어판 종료 요청됨. 창을 닫아도 됩니다.")

    def _redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
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
        if parsed.path == "/":
            assert STATE is not None
            self._write(200, render_html(STATE.snapshot()))
            return

        if parsed.path in {"/app", "/app/"}:
            asset = _resolve_react_asset("/app/index.html")
            if asset is None:
                self._write(200, render_react_placeholder())
            else:
                self._write_file(asset)
            return

        if parsed.path.startswith("/app/"):
            asset = _resolve_react_asset(parsed.path)
            if asset is not None:
                self._write_file(asset)
                return

            index_asset = _resolve_react_asset("/app/index.html")
            if index_asset is not None:
                self._write_file(index_asset)
                return

            self._write(404, "React build not found", "text/plain; charset=utf-8")
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
            self._serve_event_stream()
            return

        self._write(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/"):
            if parsed.path == "/api/start":
                self._action_start(api_mode=True)
                return
            if parsed.path == "/api/pause":
                self._action_pause(api_mode=True)
                return
            if parsed.path == "/api/resume":
                self._action_resume(api_mode=True)
                return
            if parsed.path == "/api/stop":
                self._action_stop(api_mode=True)
                return
            if parsed.path == "/api/refresh":
                self._action_refresh(api_mode=True)
                return
            if parsed.path == "/api/exit":
                self._action_exit(api_mode=True)
                return
            if parsed.path == "/api/clear_history":
                self._action_clear_history(api_mode=True)
                return
            if parsed.path == "/api/notification":
                fields = self._read_form_fields()
                selection = fields.get("selection", "")
                apply_now_raw = fields.get("apply_now")
                if apply_now_raw is None or apply_now_raw == "":
                    apply_now = True
                else:
                    apply_now = apply_now_raw.strip().lower() in {"1", "true", "yes", "on"}
                self._action_notification_update(selection, apply_now=apply_now, api_mode=True)
                return
            self._api_error(404, "Not Found")
            return

        if parsed.path == "/action/start":
            self._action_start(api_mode=False)
            return
        if parsed.path == "/action/pause":
            self._action_pause(api_mode=False)
            return
        if parsed.path == "/action/resume":
            self._action_resume(api_mode=False)
            return
        if parsed.path == "/action/stop":
            self._action_stop(api_mode=False)
            return
        if parsed.path == "/action/refresh":
            self._action_refresh(api_mode=False)
            return
        if parsed.path == "/action/exit":
            self._action_exit(api_mode=False)
            return
        if parsed.path == "/action/clear_history":
            self._action_clear_history(api_mode=False)
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
