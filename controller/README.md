# Lecture STT Go controller

`lecture-stt-controller` is the explicit operational execution owner. It keeps
the existing Python pipeline as the only code allowed to open the jobs DB,
claim a row, transcribe, or use the established staging/audio/transcript/error
file lifecycle. Go owns cadence, exact plan verification, bounded dispatch,
process fencing, interruption cleanup, and the operator kill switch.

One ownership cycle:

1. refuses to run unless both `--enable-execution` and `--allow-write` are
   present, `--max-fresh-jobs` is exactly `1`, and the state directory is an
   owned mode-0700 real directory;
2. treats an owned regular kill-switch marker as a successful idle cycle and
   rejects invalid marker types;
3. removes only stale controller-owned plan manifests after validating their
   type, owner, mode, and link count;
4. asks Python for the next exact retry plan and dispatches it through the
   guarded retry apply contract when present;
5. runs the existing Go/Python lockstep stability comparison, asks Python to
   plan each mutually stable fresh source, verifies the closed plan in Go, and
   re-plans immediately before dispatch;
6. writes a 0600, fsynced, transient manifest and calls the existing Python
   single/retry apply path with `expected_count=1`, its exact SHA-256, the
   required controller kill switch, and both Python write guards.

The Python apply path rechecks pause, kill switch, DB/source/output/config
evidence, and exact conditional claim fences immediately before ownership.
Interrupted `PROCESSING` rows continue to use Python's existing startup
recovery and must be freshly planned before replay. A controller crash may
leave only a derived plan manifest; the next cycle validates and removes it
before asking Python for current evidence. There is no automatic title or
classification promotion, upload endpoint, or new rename/move policy.

Build and run a single isolated cycle:

```sh
go build -o /tmp/lecture-stt-controller ./cmd/lecture-stt-controller
/tmp/lecture-stt-controller \
  --python-bin /path/to/lecture_stt/.venv/bin/python \
  --repo-root /path/to/lecture_stt \
  --config /path/to/config.yaml \
  --kill-switch /path/to/controller.disabled \
  --state-dir /path/to/owned-mode-0700-state \
  --max-fresh-jobs 1 \
  --enable-execution \
  --allow-write \
  --once
```

The operational LaunchAgent template is
`controller/launchd/com.geonha.lecture-stt-controller.plist.template`. It is
deliberately not installed by `scripts/setup_launchd.sh`; operational
installation and cutover are explicit operator actions. The web panel uses
`app.execution_owner: controller` and `app.controller_kill_switch` without
spawning a long-running Python worker. With `controller_runtime: launchd` it
controls `controller_label`. With `controller_runtime: console`, it instead
requires an exact 0500 binary plus SHA-256, an owned 0700 state directory, a
separate 0600 PID file outside that recovery directory, and owned log paths.
The binary must be below a SHA-256-named 0500 version directory in a 0500
`versions` store, and all three paths must carry Darwin's user-immutable flag.
It starts one detached process group, persists/fsyncs a
`lecture-stt/controller-pid@2` PID plus Darwin `proc_pidinfo` kernel
`(tv_sec,tv_usec)` birth fence, and revalidates that tuple, exact
executable/argv, and `pbi_pgid == pid` during adoption and before TERM/KILL.
This mode exists for Darwin hosts where a newly launched background service is
denied FileProvider access but the local operator console is already authorized.

Operational ownership reports use
`lecture-stt/controller-ownership-report@2`. The production command acquires
direct-child metadata through Python `stat-scan`, runs the Go stability tracker
against that projection and the canonical Python `PollingWatcher`, validates
the closed plan contract/digest in Go, and relies on the immediate guarded
Python re-plan/apply for live source evidence. Reports disclose both boundaries
as `python_stat_projection_go_stability_tracker` and
`go_contract_then_python_live_replan_apply`; they do not claim an independent
Go iCloud filesystem observation.

Rollback is stop-first and reversible: create and fsync the kill-switch marker,
terminate the console-owned process group or boot out the controller label,
set `app.execution_owner` back to `python`, bootstrap the preserved legacy
`com.geonha.lecture-stt` plist, then restart the web panel so it reloads
configuration. The kill switch blocks new claims; it does not undo a Python
transcription that already acquired its exact claim.

## Read-only controller shadow

`lecture-stt-shadow` is a bounded, read-only compatibility checker for a future
Go controller. The standalone shadow binary remains read-only: it does not
execute jobs, open the application database, claim files, or call the
single-job apply path.

For each run it:

1. asks the Python read-only probe for the normalized watcher settings;
2. lets Go own a bounded cadence and advances one long-lived canonical Python
   watcher through a closed stdin/stdout lockstep protocol;
3. compares stable direct-child filenames per scan;
4. immediately asks Python to plan each candidate observed by both sides in
   that scan, before later churn can rename or delete it;
5. independently verifies the closed plan SHA-256 and live source evidence in
   Go.

The JSON report contains relative filenames and plan digests, but not absolute
watch paths, transcript content, or source-content hashes. Report schema
`lecture-stt/controller-shadow-report@4` includes the originating `scan_index`
for every plan check, including repeated observations of the same filename,
states whether an optional kill-switch path was configured, and adds a random
128-bit `run_id` plus UTC `started_at`/`completed_at`. The run metadata lets an
evidence verifier reject copied IDs, reordered records, and overlapping runs.

Build and run from `controller/`:

```sh
go build -o /tmp/lecture-stt-shadow ./cmd/lecture-stt-shadow
/tmp/lecture-stt-shadow \
  --repo-root /path/to/lecture_stt \
  --config /path/to/config.yaml \
  --kill-switch /path/to/absent/controller.disabled
```

Use `--watch-folder`, `--scan-count`, and `--sleep-sec` only for an isolated
shadow/canary. No execution flags exist. An absent `--kill-switch` marker lets
the read-only run continue; an existing regular single-link marker stops it.
Symlink, directory, hardlink, or uninspectable marker states fail closed.
SIGINT/SIGTERM emits an `interrupted` report, exits 130, and terminates/reaps
the Python probe process group, including descendants.

## Controller ownership acceptance gate

The shadow evidence used before operational ownership was enabled remains a
separate acceptance record. It did not itself authorize claim, DB access,
execution, polling replacement, or launchd installation. The promotion gate
required all of these to be recorded:

1. On the target Darwin filesystem, at least 20 consecutive isolated churn
   runs and 100 stable observations complete with zero Python/Go scan
   mismatches and zero rejected plans for mutually observed safe files.
2. The fixture covers create, repeated append/reset, delete-before-stable,
   ignored dot/tilde/tmp/part files, same-directory rename, Unicode filenames,
   and repeated observation. Symlink, hardlink, non-regular, source-change, and
   malformed protocol cases remain 100% fail-closed.
3. Reports contain no absolute root, transcript/audio body, source-content
   digest, secret, or unbounded error text. The isolated output/DB state remains
   unchanged (and remains empty when seeded empty), and source identity changes
   only through the explicit isolated churn fixture.
4. Probe EOF, malformed/oversized lines, child stalls, interruption, and report
   observation limits terminate without orphan child processes. Lockstep scan
   response and final child close paths have bounded timeouts; command-level
   SIGTERM coverage checks the probe parent, child, and grandchild are all gone
   before exit.
5. Retry queue ownership, process fencing, crash recovery, rollback, launchd
   topology, and an operator-visible kill switch have separate reviewed
   contracts and explicit user authorization. The operational controller
   composes those guarded Python single/retry contracts; the historical
   readiness ledger alone is never a cutover authorization.

Run the target-Darwin acceptance gate only against the repository's isolated
fixtures:

```sh
LECTURE_STT_RUN_DARWIN_ACCEPTANCE=1 go test \
  ./internal/shadow -run TestRunDarwinAcceptance20ConsecutiveRuns -count=1
```

The gate performs 20 consecutive temporary-root runs and requires exactly five
verified safe-file plan observations per run, for 100 total, with polling
mismatch and safe-plan rejection both zero. It also verifies the isolated
DB/output sentinels remain unchanged and reports contain no absolute
repo/watch/DB path.

## Readiness evidence verifier

One complete Go JSON report per line can be checked without opening the worker
DB, watch root, launchd, or any report-referenced path:

```sh
PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_readiness \
  --input /path/to/shadow-evidence.jsonl \
  --min-runs 20 \
  --min-verified 100
```

The verifier accepts only a bounded, no-follow, regular single-link UTF-8 JSONL
file. Every line must be an exact successful `report@4`: read-only mode,
configured kill switch, matching contiguous scans, verified plans linked to
their stable scan, unique run ID, strictly increasing time, and no overlapping
run interval. Its metadata-only
`lecture-stt/controller-readiness-summary@1` returns only criteria, aggregate
counts, and the exact ledger SHA-256; it never returns the input path,
filenames, timestamps, run IDs, plan digests, or report bodies.

The Darwin acceptance test feeds its 20 real Go reports into this Python
verifier before passing. `.github/workflows/ci.yml` runs the Python contracts,
Go race/vet, and this 20x100 gate on macOS. The ledger digest detects changes
only after the digest is recorded; it is not a signature or proof of external
provenance.

## Temporary immutable attempt journal

Do not promote the candidate launcher stdout into a readiness ledger. For an
isolated canary, the wrapper writes a durable start record before
each Go execution and a separate finish record afterward:

```sh
PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_evidence run \
  --journal-root /path/inside/system-temp/evidence \
  --controller-bin /path/inside/system-temp/lecture-stt-shadow \
  --python-bin /path/to/lecture_stt/.venv/bin/python \
  --repo-root /path/to/lecture_stt \
  --config /path/inside/system-temp/config.yaml \
  --kill-switch /path/inside/system-temp/controller.disabled \
  --timeout-sec 1800 \
  --enable-observation \
  --allow-write \
  --expected-count 1

PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_evidence verify \
  --journal-root /path/inside/system-temp/evidence \
  --min-runs 20 \
  --min-verified 100
```

The owned journal root must be mode 0700, and it, the controller binary,
config, and absent kill-switch must all be inside the system temporary
directory. The existing Python executable and repository are read-only inputs
outside that boundary. A 0600 exclusive lock
serializes attempts. Each 0400 start/finish JSON record is created with
no-follow/O_EXCL, fsynced with its parent directory, and linked to the previous
record SHA-256. The finish records the bounded raw-output digest and size,
runner state, exit code, exact validated report when available, and a closed
outcome. Inputs and kill-switch absence are checked again after execution.

An incomplete start, failure, timeout, interruption, invalid or missing report,
spawn failure, or input change breaks the readiness suffix. Later attempts use
new sequence numbers; records are never overwritten, deleted, or automatically
rotated. Once the wrapper receives SIGINT or SIGTERM, that attempt remains an
`interrupted` barrier even if the child subsequently emits a valid success
report and exits zero. Verification counts only the final consecutive
successful suffix and reapplies unique-run, time-order, non-overlap, and
report-contract checks.

The candidate review plist now invokes this wrapper rather than the Go binary
directly. Its journal, copied binary, config, kill switch, synthetic home, and
logs remain inside the system temporary directory. This does not install or
invoke launchd, open the worker DB, claim a job, rename a file, or change Python
polling authority. The hash chain detects accidental mutation but is not an
external signature or trusted timestamp.

## Install-free launchd topology preflight

The future shadow service template stays outside the operational `launchd/`
directory:

```text
controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template
```

It is not referenced by `scripts/setup_launchd.sh`. Check the repository-only
topology without reading or changing `~/Library/LaunchAgents`, running
`launchctl`, opening the worker DB, or rendering a plist:

```sh
PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_topology \
  --repo-root /path/to/lecture_stt
```

The preflight closes the four existing labels and template digests, log-path
uniqueness, setup-script install list, and the candidate's exact read-only
arguments, kill switch, bounded schedule, placeholders, and low-priority
settings. It rejects unsafe repo files and returns a metadata-only
`lecture-stt/controller-topology-summary@1` with `mode=read_only` and
`install_supported=false`. There is deliberately no render, install,
bootstrap, kickstart, restart, or cutover command.

## Temporary review bundle

The candidate can be rendered only as a non-installable review bundle inside
the system temporary directory. Build the Go binary into that isolated root,
keep the review `config`, absent `kill-switch`, and synthetic `home` there as
well, then compute the exact plan. The repository remains a read-only topology
input; among paths rendered into the plist, only the existing Python
interpreter may be outside the temporary boundary. A venv-style leaf symlink
such as `.venv/bin/python` is normalized to its resolved executable target,
while source/config/kill-switch review artifacts remain no-follow regular-file
inputs:

```sh
PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_bundle plan \
  --repo-root /path/to/lecture_stt \
  --output-root /path/inside/system-temp/review \
  --source-binary /path/inside/system-temp/lecture-stt-shadow \
  --python-bin /path/to/lecture_stt/.venv/bin/python \
  --config /path/inside/system-temp/config.yaml \
  --kill-switch /path/inside/system-temp/controller.disabled \
  --home /path/inside/system-temp/home \
  --evidence-root /path/inside/system-temp/evidence
```

Preparing one bundle additionally requires `--enable-prepare`,
`--allow-write`, `--expected-count 1`, and the exact plan SHA-256. The command
replans immediately before creating `controller-shadow-review-bundle`, writes
the copied binary, `.plist.review`, and a minimal `python-runtime` containing
only the `lecture_stt` package initializers plus `controller_evidence.py` and
`controller_readiness.py`. Runtime directories are sealed to 0500, files to
0400, and the candidate fixes `PYTHONDONTWRITEBYTECODE=1` with `PYTHONPATH`
and `WorkingDirectory` both pointing at that bundled tree instead of the
mutable repository source. All artifacts use exclusive no-follow creation and
file/directory fsync, and the manifest is recorded last. Exact completed replay
returns `skipped`; partial, extra, changed, or runtime-tree-drifted artifacts
require manual recovery and are never deleted automatically.

The bundle manifest fixes `execution_supported`, `install_supported`,
`uninstall_supported`, `rollback_supported`, `launchd_install_supported`, and
`operational_install_supported` to false. It also fixes
`evidence_wrapper_connected=true`. Candidate stdout is the metadata-only
`controller-shadow.evidence-result.jsonl`; readiness comes only from the
immutable attempt journal. No bundle command copies to LaunchAgents, invokes
`launchctl`, runs the binary, opens the worker DB/root, or changes Python
polling authority.

## Temporary offline activation ledger

The prepared bundle can be exercised through an offline lifecycle ledger
without installing a service:

```sh
PYTHONPATH=src .venv/bin/python -m lecture_stt.stt.controller_activation \
  plan-install \
  --activation-root /path/inside/system-temp/activation \
  --bundle-root /path/inside/system-temp/review/controller-shadow-review-bundle
```

Apply the returned exact plan with `apply-install`, `--enable-activation`,
`--allow-write`, `--expected-count 1`, and `--expected-plan-sha256`. After an
install, `plan-uninstall`/`apply-uninstall` append an inactive tombstone without
deleting the immutable version. `plan-rollback`/`apply-rollback` require
`--target-plan-sha256` for a previously installed version.

The activation root is an owned 0700 directory. Versions are stored under their
bundle plan SHA-256 with a 0500 binary, 0400 plist/manifest, and an exact
0500/0400 copy of the bundled Python runtime. The runtime tree SHA-256 is part
of the version identity. A 0600 nonblocking lock serializes apply, and every
0400 action record closes the previous record and previous/target active state
hashes. Exact replay is skipped. A complete version left before its action
record may be recovered only through a newly approved exact plan; partial or
changed evidence, including runtime drift, requires manual recovery and is
never removed automatically.

Despite the lifecycle names, this module is `scope=temporary_offline`: it has no
LaunchAgents, `launchctl`, subprocess, worker DB/root, or restart path. Its
results always state `launchctl_invoked=false`,
`operational_install_supported=false`, and
`python_polling_authority=true`.

These temporary evidence and offline activation artifacts do not change
ownership. Operational ownership changes only through the separately
authorized launchd or console-runtime cutover described above; the Python
pipeline remains the execution engine behind the guarded Go dispatcher.
