package shadow

import (
	"context"
	"errors"
	"math"
)

const StatScanSchemaVersion = "lecture-stt/controller-stat-scan@1"

type statScanReport struct {
	Entries       []Entry `json:"entries"`
	SchemaVersion string  `json:"schema_version"`
}

func scanWatchFolderContext(
	ctx context.Context,
	options Options,
	watchFolder string,
) ([]Entry, error) {
	if !options.PythonStatScan {
		return ScanWatchFolder(watchFolder)
	}
	attempts := options.ScanHelperAttempts
	if attempts <= 0 {
		attempts = 1
	}
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		entries, err := runPythonStatScanContext(ctx, options)
		if err == nil {
			return entries, nil
		}
		if errors.Is(err, context.Canceled) {
			return nil, err
		}
		lastErr = err
	}
	return nil, lastErr
}

func runPythonStatScanContext(
	ctx context.Context,
	options Options,
) ([]Entry, error) {
	scanCtx, cancel := context.WithTimeout(ctx, options.ScanTimeout)
	defer cancel()
	value, err := runPythonContext(
		scanCtx,
		options,
		maxPlanBytes,
		"lecture_stt.stt.shadow_probe",
		"stat-scan",
		"--config",
		options.ConfigPath,
	)
	if err != nil {
		if ctx.Err() != nil {
			return nil, context.Canceled
		}
		return nil, errors.New("Python stat scan failed")
	}
	return decodeStatScanPayload(value)
}

func decodeStatScanPayload(value []byte) ([]Entry, error) {
	if len(value) == 0 || len(value) > maxPlanBytes {
		return nil, errors.New("stat scan payload size is invalid")
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return nil, err
	}
	var report statScanReport
	if err := strictDecode(value, &report); err != nil {
		return nil, err
	}
	if report.SchemaVersion != StatScanSchemaVersion || report.Entries == nil {
		return nil, errors.New("stat scan payload contract is invalid")
	}
	seen := make(map[string]struct{}, len(report.Entries))
	for _, entry := range report.Entries {
		if err := validateDirectName(entry.Name); err != nil ||
			entry.SizeBytes < 0 ||
			math.IsNaN(entry.Mtime) ||
			math.IsInf(entry.Mtime, 0) {
			return nil, errors.New("stat scan entry is invalid")
		}
		if _, exists := seen[entry.Name]; exists {
			return nil, errors.New("stat scan entry is duplicated")
		}
		seen[entry.Name] = struct{}{}
	}
	return report.Entries, nil
}
