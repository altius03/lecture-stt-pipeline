package shadow

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestTrackerMirrorsStableWindowAndResetsAfterEmission(t *testing.T) {
	tracker := NewTracker(10 * time.Second)
	start := time.Unix(100, 0)
	entry := Entry{Name: "강의.m4a", SizeBytes: 10, Mtime: 20}

	if stable := tracker.Observe([]Entry{entry}, start); len(stable) != 0 {
		t.Fatalf("first observation must seed state, got %v", stable)
	}
	if stable := tracker.Observe([]Entry{entry}, start.Add(9*time.Second)); len(stable) != 0 {
		t.Fatalf("entry became stable too early: %v", stable)
	}
	if stable := tracker.Observe([]Entry{entry}, start.Add(10*time.Second)); len(stable) != 1 {
		t.Fatalf("expected one stable entry, got %v", stable)
	}
	if stable := tracker.Observe([]Entry{entry}, start.Add(11*time.Second)); len(stable) != 0 {
		t.Fatalf("emitted entry must seed again on the next scan, got %v", stable)
	}
}

func TestTrackerResetsChangedAndMissingEntries(t *testing.T) {
	tracker := NewTracker(time.Second)
	start := time.Unix(100, 0)
	entry := Entry{Name: "audio.m4a", SizeBytes: 10, Mtime: 20}
	tracker.Observe([]Entry{entry}, start)

	changed := entry
	changed.SizeBytes++
	if stable := tracker.Observe([]Entry{changed}, start.Add(time.Second)); len(stable) != 0 {
		t.Fatalf("changed entry must reset, got %v", stable)
	}
	tracker.Observe(nil, start.Add(2*time.Second))
	if stable := tracker.Observe([]Entry{changed}, start.Add(3*time.Second)); len(stable) != 0 {
		t.Fatalf("missing entry must be treated as new, got %v", stable)
	}
}

func TestScanWatchFolderMatchesTemporaryAndDirectFilePolicy(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "강의.m4a"), []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{".hidden", "~upload", "one.tmp", "two.PART"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("partial"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(root, "nested"), 0o700); err != nil {
		t.Fatal(err)
	}

	entries, err := ScanWatchFolder(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name != "강의.m4a" {
		t.Fatalf("unexpected scan entries: %#v", entries)
	}
}
