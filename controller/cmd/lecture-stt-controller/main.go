package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"lecture-stt/controller/internal/shadow"
)

func main() {
	var options shadow.OwnershipOptions
	var once bool
	var cycleIntervalSec int
	flag.StringVar(&options.Shadow.PythonBin, "python-bin", "python3", "Python interpreter")
	flag.StringVar(&options.Shadow.RepoRoot, "repo-root", "", "Repository root")
	flag.StringVar(&options.Shadow.ConfigPath, "config", "", "Worker config path")
	flag.StringVar(&options.Shadow.KillSwitchPath, "kill-switch", "", "Required ownership kill switch")
	flag.StringVar(&options.StateDir, "state-dir", "", "Owned mode-0700 controller state directory")
	flag.BoolVar(&options.Enable, "enable-execution", false, "Explicitly enable controller ownership")
	flag.BoolVar(&options.AllowWrite, "allow-write", false, "Allow guarded Python job execution")
	flag.IntVar(&options.MaxFreshJobs, "max-fresh-jobs", 1, "Maximum fresh jobs per cycle; must be 1")
	flag.BoolVar(&once, "once", false, "Run one ownership cycle")
	flag.IntVar(&cycleIntervalSec, "cycle-interval-sec", 1, "Delay between completed cycles")
	flag.Parse()
	if flag.NArg() != 0 || cycleIntervalSec < 1 || cycleIntervalSec > 3600 {
		emit(shadow.OwnershipReport{
			SchemaVersion:    shadow.OwnershipReportSchemaVersion,
			Mode:             "controller_owned",
			ObservationMode:  "python_stat_projection_go_stability_tracker",
			PlanVerification: "go_contract_then_python_live_replan_apply",
			ErrorKind:        "invalid_options",
			Executions:       []shadow.OwnershipExecution{},
		})
		os.Exit(2)
	}
	options.Shadow.PythonStatScan = true
	options.Shadow.ScanHelperAttempts = 3
	options.Shadow.ScanTimeout = 5 * time.Second

	lockFile, err := acquireControllerLock(options.StateDir)
	if err != nil {
		emit(shadow.OwnershipReport{
			SchemaVersion:    shadow.OwnershipReportSchemaVersion,
			Mode:             "controller_owned",
			ObservationMode:  "python_stat_projection_go_stability_tracker",
			PlanVerification: "go_contract_then_python_live_replan_apply",
			ErrorKind:        "controller_busy",
			Executions:       []shadow.OwnershipExecution{},
		})
		os.Exit(75)
	}
	defer releaseControllerLock(lockFile)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	for {
		report, runErr := shadow.RunOwnershipCycleContext(ctx, options)
		emit(report)
		if once {
			if runErr != nil || !report.OK {
				os.Exit(2)
			}
			return
		}
		if errors.Is(runErr, context.Canceled) || ctx.Err() != nil {
			return
		}
		timer := time.NewTimer(time.Duration(cycleIntervalSec) * time.Second)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return
		}
	}
}

func emit(report shadow.OwnershipReport) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(report); err != nil {
		os.Exit(1)
	}
}

func acquireControllerLock(stateDir string) (*os.File, error) {
	absolute, err := filepath.Abs(stateDir)
	if err != nil {
		return nil, err
	}
	resolved, err := filepath.EvalSymlinks(filepath.Clean(absolute))
	if err != nil {
		return nil, err
	}
	info, err := os.Lstat(resolved)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o700 {
		return nil, errors.New("invalid controller state directory")
	}
	statValue, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(statValue.Uid) != os.Geteuid() {
		return nil, errors.New("invalid controller state directory")
	}
	lockPath := filepath.Join(resolved, "controller.lock")
	file, err := os.OpenFile(
		lockPath,
		os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC,
		0o600,
	)
	if err != nil {
		return nil, err
	}
	opened, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, err
	}
	linked, err := os.Lstat(lockPath)
	if err != nil ||
		!opened.Mode().IsRegular() ||
		opened.Mode().Perm() != 0o600 ||
		opened.Mode()&os.ModeSymlink != 0 ||
		!os.SameFile(opened, linked) {
		file.Close()
		return nil, errors.New("invalid controller lock file")
	}
	lockStat, ok := opened.Sys().(*syscall.Stat_t)
	if !ok || int(lockStat.Uid) != os.Geteuid() || lockStat.Nlink != 1 {
		file.Close()
		return nil, errors.New("invalid controller lock file")
	}
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		file.Close()
		return nil, err
	}
	return file, nil
}

func releaseControllerLock(file *os.File) {
	if file == nil {
		return
	}
	_ = unix.Flock(int(file.Fd()), unix.LOCK_UN)
	_ = file.Close()
}
