package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"math"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"lecture-stt/controller/internal/shadow"
)

type optionalFloat struct {
	set   bool
	value float64
}

func (value *optionalFloat) String() string {
	if !value.set {
		return ""
	}
	return strconv.FormatFloat(value.value, 'g', -1, 64)
}

func (value *optionalFloat) Set(raw string) error {
	parsed, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return errors.New("must be a number")
	}
	if math.IsNaN(parsed) || math.IsInf(parsed, 0) {
		return errors.New("must be finite")
	}
	value.value = parsed
	value.set = true
	return nil
}

func main() {
	var sleep optionalFloat
	options := shadow.Options{}
	flag.StringVar(&options.PythonBin, "python-bin", "python3", "Python interpreter for read-only probes")
	flag.StringVar(&options.RepoRoot, "repo-root", "", "Repository root containing src/lecture_stt")
	flag.StringVar(&options.ConfigPath, "config", "", "Worker config path")
	flag.StringVar(&options.WatchFolderOverride, "watch-folder", "", "Optional read-only watch-folder override")
	flag.StringVar(
		&options.KillSwitchPath,
		"kill-switch",
		"",
		"Optional marker path; an existing regular single-link marker stops the shadow",
	)
	flag.IntVar(&options.ScanCount, "scan-count", 0, "Bounded scans; 0 derives a closed count from timing")
	flag.Var(&sleep, "sleep-sec", "Optional interval between bounded scans")
	flag.Parse()
	if flag.NArg() != 0 {
		emitFailure("invalid_options")
		os.Exit(2)
	}
	if sleep.set {
		options.SleepSec = &sleep.value
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	report, err := shadow.RunContext(ctx, options)
	if err != nil {
		if report.ErrorKind == "" {
			report.ErrorKind = "shadow_failed"
		}
		emit(report)
		if errors.Is(err, context.Canceled) {
			os.Exit(130)
		}
		os.Exit(2)
	}
	emit(report)
	if !report.OK {
		os.Exit(2)
	}
}

func emit(report shadow.Report) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(report); err != nil {
		os.Exit(1)
	}
}

func emitFailure(kind string) {
	emit(shadow.NewFailureReport(kind))
}
