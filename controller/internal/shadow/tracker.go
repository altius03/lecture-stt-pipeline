package shadow

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type Entry struct {
	Name      string  `json:"name"`
	SizeBytes int64   `json:"size_bytes"`
	Mtime     float64 `json:"mtime"`
}

type fileState struct {
	sizeBytes       int64
	mtime           float64
	firstUnstableAt time.Time
}

type Tracker struct {
	stableFor time.Duration
	states    map[string]fileState
}

func NewTracker(stableFor time.Duration) *Tracker {
	return &Tracker{
		stableFor: stableFor,
		states:    make(map[string]fileState),
	}
}

func (t *Tracker) Observe(entries []Entry, now time.Time) []string {
	observed := make(map[string]struct{}, len(entries))
	stable := make([]string, 0)

	for _, entry := range entries {
		observed[entry.Name] = struct{}{}
		state, ok := t.states[entry.Name]
		if !ok || state.sizeBytes != entry.SizeBytes || state.mtime != entry.Mtime {
			t.states[entry.Name] = fileState{
				sizeBytes:       entry.SizeBytes,
				mtime:           entry.Mtime,
				firstUnstableAt: now,
			}
			continue
		}
		if now.Sub(state.firstUnstableAt) >= t.stableFor {
			stable = append(stable, entry.Name)
			delete(t.states, entry.Name)
		}
	}

	for name := range t.states {
		if _, ok := observed[name]; !ok {
			delete(t.states, name)
		}
	}
	sort.Strings(stable)
	return stable
}

func ScanWatchFolder(watchFolder string) ([]Entry, error) {
	dirEntries, err := os.ReadDir(watchFolder)
	if err != nil {
		return nil, err
	}
	entries := make([]Entry, 0, len(dirEntries))
	for _, dirEntry := range dirEntries {
		if isTemporaryName(dirEntry.Name()) {
			continue
		}
		info, err := os.Stat(filepath.Join(watchFolder, dirEntry.Name()))
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		modified := info.ModTime()
		entries = append(entries, Entry{
			Name:      dirEntry.Name(),
			SizeBytes: info.Size(),
			Mtime:     float64(modified.Unix()) + float64(modified.Nanosecond())/float64(time.Second),
		})
	}
	return entries, nil
}

func isTemporaryName(name string) bool {
	if strings.HasPrefix(name, ".") || strings.HasPrefix(name, "~") {
		return true
	}
	lower := strings.ToLower(name)
	return strings.HasSuffix(lower, ".tmp") || strings.HasSuffix(lower, ".part")
}
