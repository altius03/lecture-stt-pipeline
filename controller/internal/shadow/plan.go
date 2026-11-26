package shadow

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

const (
	PlanSchemaVersion = "lecture-stt/single-job-plan@1"
	maxPlanBytes      = 64 * 1024
)

type ProfileEvidence struct {
	ConfigSHA256 string `json:"config_sha256"`
	Key          string `json:"key"`
	Version      string `json:"version"`
}

type WorkerEvidence struct {
	ConfigSHA256 string          `json:"config_sha256"`
	Profile      ProfileEvidence `json:"profile"`
}

type SourceEvidence struct {
	CtimeNS      int64  `json:"ctime_ns"`
	Device       uint64 `json:"device"`
	Inode        uint64 `json:"inode"`
	MtimeNS      int64  `json:"mtime_ns"`
	RelativePath string `json:"relative_path"`
	SHA256       string `json:"sha256"`
	SizeBytes    int64  `json:"size_bytes"`
}

type Plan struct {
	ExpectedCount int            `json:"expected_count"`
	PlanSHA256    string         `json:"plan_sha256"`
	SchemaVersion string         `json:"schema_version"`
	Source        SourceEvidence `json:"source"`
	Worker        WorkerEvidence `json:"worker"`
}

type ContractError struct {
	Kind string
	Err  error
}

func (e *ContractError) Error() string {
	return fmt.Sprintf("%s: %v", e.Kind, e.Err)
}

func (e *ContractError) Unwrap() error {
	return e.Err
}

func DecodeAndVerifyPlan(planBytes []byte, watchFolder string) (Plan, error) {
	plan, err := DecodeAndVerifyPlanContract(planBytes)
	if err != nil {
		return Plan{}, err
	}
	live, err := readSourceEvidence(watchFolder, plan.Source.RelativePath)
	if err != nil {
		return Plan{}, &ContractError{Kind: "source_rejected", Err: err}
	}
	if live != plan.Source {
		return Plan{}, &ContractError{
			Kind: "evidence_mismatch",
			Err:  errors.New("Go source evidence does not match Python source evidence"),
		}
	}
	return plan, nil
}

func DecodeAndVerifyPlanContract(planBytes []byte) (Plan, error) {
	if len(planBytes) == 0 || len(planBytes) > maxPlanBytes {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: errors.New("plan size is invalid")}
	}
	if err := rejectDuplicateJSONKeys(planBytes); err != nil {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: err}
	}
	var plan Plan
	decoder := json.NewDecoder(bytes.NewReader(planBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&plan); err != nil {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: err}
	}
	if err := requireJSONEOF(decoder); err != nil {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: err}
	}
	if err := validatePlan(plan); err != nil {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: err}
	}

	payload, err := canonicalPlanPayload(plan)
	if err != nil {
		return Plan{}, &ContractError{Kind: "invalid_plan", Err: err}
	}
	digest := sha256.Sum256(payload)
	if hex.EncodeToString(digest[:]) != plan.PlanSHA256 {
		return Plan{}, &ContractError{
			Kind: "digest_mismatch",
			Err:  errors.New("Go canonical plan digest does not match Python plan_sha256"),
		}
	}

	return plan, nil
}

func validatePlan(plan Plan) error {
	if plan.SchemaVersion != PlanSchemaVersion {
		return fmt.Errorf("unsupported schema_version %q", plan.SchemaVersion)
	}
	if plan.ExpectedCount != 1 {
		return errors.New("expected_count must equal 1")
	}
	if err := validateSHA256(plan.PlanSHA256); err != nil {
		return fmt.Errorf("plan_sha256: %w", err)
	}
	if err := validateSHA256(plan.Source.SHA256); err != nil {
		return fmt.Errorf("source.sha256: %w", err)
	}
	if err := validateSHA256(plan.Worker.ConfigSHA256); err != nil {
		return fmt.Errorf("worker.config_sha256: %w", err)
	}
	if err := validateSHA256(plan.Worker.Profile.ConfigSHA256); err != nil {
		return fmt.Errorf("worker.profile.config_sha256: %w", err)
	}
	if plan.Worker.Profile.Key == "" || plan.Worker.Profile.Version == "" {
		return errors.New("worker profile key and version must be non-empty")
	}
	if plan.Source.SizeBytes < 0 || plan.Source.MtimeNS < 0 || plan.Source.CtimeNS < 0 {
		return errors.New("source numeric evidence must be non-negative")
	}
	return validateDirectName(plan.Source.RelativePath)
}

func validateSHA256(value string) error {
	if len(value) != 64 {
		return errors.New("must be a lowercase SHA-256")
	}
	for _, character := range value {
		if !strings.ContainsRune("0123456789abcdef", character) {
			return errors.New("must be a lowercase SHA-256")
		}
	}
	return nil
}

func validateDirectName(name string) error {
	if name == "" || name == "." || name == ".." || filepath.IsAbs(name) {
		return errors.New("source path must be one direct watch-folder child")
	}
	if filepath.Base(name) != name || strings.ContainsAny(name, `/\`+"\x00") {
		return errors.New("source path must be one direct watch-folder child")
	}
	if isTemporaryName(name) {
		return errors.New("source path is excluded by the polling watcher")
	}
	return nil
}

func canonicalPlanPayload(plan Plan) ([]byte, error) {
	payload := map[string]any{
		"expected_count": plan.ExpectedCount,
		"schema_version": plan.SchemaVersion,
		"source": map[string]any{
			"ctime_ns":      plan.Source.CtimeNS,
			"device":        plan.Source.Device,
			"inode":         plan.Source.Inode,
			"mtime_ns":      plan.Source.MtimeNS,
			"relative_path": plan.Source.RelativePath,
			"sha256":        plan.Source.SHA256,
			"size_bytes":    plan.Source.SizeBytes,
		},
		"worker": map[string]any{
			"config_sha256": plan.Worker.ConfigSHA256,
			"profile": map[string]any{
				"config_sha256": plan.Worker.Profile.ConfigSHA256,
				"key":           plan.Worker.Profile.Key,
				"version":       plan.Worker.Profile.Version,
			},
		},
	}
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(payload); err != nil {
		return nil, err
	}
	canonical := bytes.TrimSuffix(buffer.Bytes(), []byte("\n"))
	// Python's ensure_ascii=False leaves these valid Unicode code points
	// unescaped, while encoding/json escapes them even with HTML escaping off.
	return unescapeJSONLineSeparators(canonical), nil
}

func unescapeJSONLineSeparators(value []byte) []byte {
	result := make([]byte, 0, len(value))
	for index := 0; index < len(value); {
		if index+6 <= len(value) &&
			value[index] == '\\' &&
			(bytes.Equal(value[index:index+6], []byte(`\u2028`)) ||
				bytes.Equal(value[index:index+6], []byte(`\u2029`))) {
			precedingSlashes := 0
			for prior := index - 1; prior >= 0 && value[prior] == '\\'; prior-- {
				precedingSlashes++
			}
			if precedingSlashes%2 == 0 {
				if value[index+5] == '8' {
					result = append(result, []byte("\u2028")...)
				} else {
					result = append(result, []byte("\u2029")...)
				}
				index += 6
				continue
			}
		}
		result = append(result, value[index])
		index++
	}
	return result
}

func requireJSONEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("unexpected trailing JSON value")
		}
		return err
	}
	return nil
}

func rejectDuplicateJSONKeys(value []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(value))
	decoder.UseNumber()
	var walk func() error
	walk = func() error {
		token, err := decoder.Token()
		if err != nil {
			return err
		}
		delim, ok := token.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := make(map[string]struct{})
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return err
				}
				key, ok := keyToken.(string)
				if !ok {
					return errors.New("JSON object key must be a string")
				}
				if _, exists := seen[key]; exists {
					return fmt.Errorf("duplicate JSON key %q", key)
				}
				seen[key] = struct{}{}
				if err := walk(); err != nil {
					return err
				}
			}
			end, err := decoder.Token()
			if err != nil {
				return err
			}
			if end != json.Delim('}') {
				return errors.New("malformed JSON object")
			}
		case '[':
			for decoder.More() {
				if err := walk(); err != nil {
					return err
				}
			}
			end, err := decoder.Token()
			if err != nil {
				return err
			}
			if end != json.Delim(']') {
				return errors.New("malformed JSON array")
			}
		default:
			return errors.New("unexpected JSON delimiter")
		}
		return nil
	}
	if err := walk(); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("unexpected trailing JSON value")
		}
		return err
	}
	return nil
}

func readSourceEvidence(watchFolder, relativePath string) (SourceEvidence, error) {
	if err := validateDirectName(relativePath); err != nil {
		return SourceEvidence{}, err
	}
	rootFD, err := syscall.Open(
		watchFolder,
		syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC,
		0,
	)
	if err != nil {
		return SourceEvidence{}, fmt.Errorf("open watch folder: %w", err)
	}
	defer syscall.Close(rootFD)

	sourceFD, err := unix.Openat(
		rootFD,
		relativePath,
		syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC,
		0,
	)
	if err != nil {
		return SourceEvidence{}, fmt.Errorf("open source: %w", err)
	}
	file := os.NewFile(uintptr(sourceFD), relativePath)
	if file == nil {
		syscall.Close(sourceFD)
		return SourceEvidence{}, errors.New("wrap source descriptor")
	}
	defer file.Close()

	var before syscall.Stat_t
	if err := syscall.Fstat(sourceFD, &before); err != nil {
		return SourceEvidence{}, fmt.Errorf("fstat source before hash: %w", err)
	}
	beforeIdentity := identityFromStat(&before)
	if beforeIdentity.mode&syscall.S_IFMT != syscall.S_IFREG {
		return SourceEvidence{}, errors.New("source must be a regular file")
	}
	if beforeIdentity.links != 1 {
		return SourceEvidence{}, errors.New("source must have exactly one hard link")
	}

	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return SourceEvidence{}, fmt.Errorf("hash source: %w", err)
	}
	var after syscall.Stat_t
	if err := syscall.Fstat(sourceFD, &after); err != nil {
		return SourceEvidence{}, fmt.Errorf("fstat source after hash: %w", err)
	}
	afterIdentity := identityFromStat(&after)
	var current syscall.Stat_t
	if err := syscall.Lstat(filepath.Join(watchFolder, relativePath), &current); err != nil {
		return SourceEvidence{}, fmt.Errorf("lstat source pathname: %w", err)
	}
	currentIdentity := identityFromStat(&current)
	if beforeIdentity != afterIdentity || afterIdentity != currentIdentity {
		return SourceEvidence{}, errors.New("source changed while Go shadow hashed it")
	}

	return SourceEvidence{
		CtimeNS:      afterIdentity.ctimeNS,
		Device:       afterIdentity.device,
		Inode:        afterIdentity.inode,
		MtimeNS:      afterIdentity.mtimeNS,
		RelativePath: relativePath,
		SHA256:       hex.EncodeToString(digest.Sum(nil)),
		SizeBytes:    afterIdentity.sizeBytes,
	}, nil
}
