"""Record-centric storage v2 primitives.

The package is intentionally isolated from the active v1 worker. Importing it
does not create a database, move files, or change the current runtime.
"""

from lecture_stt.storage_v2.importer import (
    ArtifactSource,
    ImportIssue,
    ImportPlan,
    ImportResult,
    LegacyCandidate,
    discover_candidates,
    import_candidate,
)
from lecture_stt.storage_v2.analytics import (
    disabled_transcription_analytics,
    read_transcription_analytics,
)
from lecture_stt.storage_v2.library import (
    RecordingLibraryDisabledError,
    RecordingLibraryNotFoundError,
    disabled_recording_library_list,
    list_recordings,
    read_recording_detail,
)
from lecture_stt.storage_v2.repository import (
    apply_migration,
    read_library_snapshot,
)
from lecture_stt.storage_v2.verifier import verify_library

__all__ = [
    "ArtifactSource",
    "ImportIssue",
    "ImportPlan",
    "ImportResult",
    "LegacyCandidate",
    "RecordingLibraryDisabledError",
    "RecordingLibraryNotFoundError",
    "disabled_transcription_analytics",
    "disabled_recording_library_list",
    "apply_migration",
    "discover_candidates",
    "import_candidate",
    "list_recordings",
    "read_library_snapshot",
    "read_recording_detail",
    "read_transcription_analytics",
    "verify_library",
]
