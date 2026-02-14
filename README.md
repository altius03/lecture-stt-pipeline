# Lecture STT Pipeline

This repository runs an always-on, polling-based speech-to-text worker for macOS lecture recordings.

## Features

- Polls `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox` every 10 seconds.
- Waits for `stable_for_sec=90` before processing each file.
- Moves stable files to `01_audio` and renames to `YYYYMMDD_HHMMSS__{orig_stem_sanitized}__{shortid}.{ext}`.
- Runs STT with `faster-whisper` using:
  - `model_size=large-v3`
  - `device=cpu`
  - `compute_type=int8`
  - `language=ko`
  - `task=transcribe`
  - `beam_size=5`
  - `vad_filter=false`
  - `word_timestamps=false`
- Writes UTF-8:
  - `<canonical_base>.txt`
  - `<canonical_base>.json`
- Stores job state in SQLite with `PENDING/PROCESSING/DONE/ERROR`.
- Deduplicates by SHA-256, reusing transcripts for duplicate DONE jobs.
- Sends Discord webhook notifications on ERROR.
- Auto-recovers interrupted `PROCESSING` jobs on startup.

## Exact environment layout

The defaults are in `config/config.example.yaml` and should match:

- Inbox: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox`
- Audio: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/01_audio`
- Transcripts: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts`
- Errors: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/99_errors`
- Temp: `/Users/geonha/lecture_stt/tmp`
- DB: `/Users/geonha/lecture_stt/state/jobs.sqlite3`

## Setup (pyenv + Python 3.12.8)

```bash
cd /Users/geonha/lecture_stt
pyenv install 3.12.8
pyenv local 3.12.8
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If `/Users/geonha/lecture_stt/.venv/bin/python` does not exist, create/install it first and then run again.

## Configure

```bash
cp config/config.example.yaml config/config.yaml
# edit for your paths / host as needed
cp .env.example .env
# .env content
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

`config/config.yaml` is required. The pipeline fails at startup if config is missing, empty, or missing required keys.

## Pause/Resume/Status

```bash
python src/main.py --pause   # stop scanning and processing new files
python src/main.py --resume  # resume scanning and processing
python src/main.py --status  # show current pause state
```

Pause flag location:

`/Users/geonha/lecture_stt/state/paused`

## Discord notifications (Korean)

Both 성공/실패 알림을 한국어로 보냅니다.  
`DISCORD_WEBHOOK_URL`이 설정되어 있어야 하며, 미설정이면 알림은 건너뜁니다.

성공 메시지는 `DONE` 전환 및 결과 파일 저장 후 전송되고,
실패 메시지는 예외 발생 시 전송됩니다.

중복 알림 방지를 위해 아래 마커 파일을 사용합니다.

- `state/notified/success_<job_id>`
- `state/notified/error_<job_id>`

성공/실패 각각 한 번만 POST 되고, 같은 `job_id`는 재시작해도 재전송되지 않습니다.

## Run manually

```bash
source .venv/bin/activate
python scripts/download_model.py
bash scripts/run_once.sh           # run one scan and process max one job
python src/main.py                 # run daemon mode
```

## Run daemon with launchd (user agent)

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.geonha.lecture-stt.plist ~/Library/LaunchAgents/
cp launchd/com.geonha.lecture-stt-cleanup.plist ~/Library/LaunchAgents/

# load
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.geonha.lecture-stt.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.geonha.lecture-stt-cleanup.plist

# unload
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.geonha.lecture-stt.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.geonha.lecture-stt-cleanup.plist

# restart (optional)
launchctl kickstart -k gui/$(id -u)/com.geonha.lecture-stt
launchctl kickstart -k gui/$(id -u)/com.geonha.lecture-stt-cleanup
```

## Run cleanup manually

```bash
python scripts/cleanup.py            # safe dry-run (default)
python scripts/cleanup.py --apply      # perform deletions
```

## Logs + troubleshoot

- Main logs: `logs/app.log`
- launchd output/error logs:
  - `state/logs/launchd.out.log`
  - `state/logs/launchd.err.log`
  - `state/logs/cleanup.out.log`
  - `state/logs/cleanup.err.log`

```bash
tail -f /Users/geonha/lecture_stt/state/logs/launchd.out.log
tail -f /Users/geonha/lecture_stt/state/logs/launchd.err.log
tail -f /Users/geonha/lecture_stt/state/logs/cleanup.out.log
tail -f /Users/geonha/lecture_stt/state/logs/cleanup.err.log
tail -f /Users/geonha/lecture_stt/logs/app.log
```

Useful checks:

```bash
sqlite3 /Users/geonha/lecture_stt/state/jobs.sqlite3 "SELECT id,status,canonical_base,orig_name,sha256,started_at,ended_at,preprocess_sec,transcribe_sec,total_sec FROM jobs ORDER BY id DESC LIMIT 20;"
```

## How to run first smoke test

1. Start one-off warm run:
   - `bash scripts/run_once.sh`
2. Drop one audio file (for example `.m4a`) into:
   `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox`
3. Wait at least `90` seconds for stability.
4. Confirm the file moved to `01_audio` and renamed as:
   `YYYYMMDD_HHMMSS__{orig_stem_sanitized}__{8_chars}.{ext}`.
5. Confirm transcript files exist:
   - `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts/<canonical_base>.txt`
   - `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts/<canonical_base>.json`
6. Confirm DB row exists and status is `DONE`.

## Notes

- If `/Volumes` is unavailable in your sandbox/workspace, keep the absolute paths as-is in code/config.
- `logs/` and `state/` directories are runtime artifacts and should not be committed.
