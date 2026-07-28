package shadow

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func validPlanForFile(t *testing.T, root, name string) Plan {
	t.Helper()
	source, err := readSourceEvidence(root, name)
	if err != nil {
		t.Fatal(err)
	}
	plan := Plan{
		ExpectedCount: 1,
		SchemaVersion: PlanSchemaVersion,
		Source:        source,
		Worker: WorkerEvidence{
			ConfigSHA256: strings.Repeat("a", 64),
			Profile: ProfileEvidence{
				ConfigSHA256: strings.Repeat("b", 64),
				Key:          "default",
				Version:      "1",
			},
		},
	}
	payload, err := canonicalPlanPayload(plan)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(payload)
	plan.PlanSHA256 = hex.EncodeToString(digest[:])
	return plan
}

func encodePlan(t *testing.T, plan Plan) []byte {
	t.Helper()
	value, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func requireContractKind(t *testing.T, err error, kind string) {
	t.Helper()
	var contractErr *ContractError
	if !errors.As(err, &contractErr) {
		t.Fatalf("expected ContractError, got %T: %v", err, err)
	}
	if contractErr.Kind != kind {
		t.Fatalf("expected kind %q, got %q", kind, contractErr.Kind)
	}
}

func TestDecodeAndVerifyPlanAcceptsIndependentEvidence(t *testing.T) {
	root := t.TempDir()
	name := "강의\u2028메모.m4a"
	if err := os.WriteFile(filepath.Join(root, name), []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	plan := validPlanForFile(t, root, name)

	verified, err := DecodeAndVerifyPlan(encodePlan(t, plan), root)
	if err != nil {
		t.Fatal(err)
	}
	if verified.PlanSHA256 != plan.PlanSHA256 {
		t.Fatalf("unexpected plan digest: %s", verified.PlanSHA256)
	}

	canonical, err := canonicalPlanPayload(plan)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(canonical), `\u2028`) {
		t.Fatalf("canonical JSON must match Python ensure_ascii=False: %s", canonical)
	}
}

func TestCanonicalPlanPreservesLiteralBackslashUnicodeText(t *testing.T) {
	plan := Plan{
		ExpectedCount: 1,
		SchemaVersion: PlanSchemaVersion,
		Source: SourceEvidence{
			RelativePath: "audio.m4a",
			SHA256:       strings.Repeat("a", 64),
		},
		Worker: WorkerEvidence{
			ConfigSHA256: strings.Repeat("b", 64),
			Profile: ProfileEvidence{
				ConfigSHA256: strings.Repeat("c", 64),
				Key:          `literal\u2028`,
				Version:      "1",
			},
		},
	}
	canonical, err := canonicalPlanPayload(plan)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(canonical, []byte(`"key":"literal\\u2028"`)) {
		t.Fatalf("literal backslash sequence was corrupted: %s", canonical)
	}
}

func TestDecodeAndVerifyPlanRejectsDigestTamper(t *testing.T) {
	root := t.TempDir()
	name := "audio.m4a"
	if err := os.WriteFile(filepath.Join(root, name), []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	plan := validPlanForFile(t, root, name)
	plan.Worker.Profile.Version = "tampered"

	_, err := DecodeAndVerifyPlan(encodePlan(t, plan), root)
	requireContractKind(t, err, "digest_mismatch")
}

func TestDecodeAndVerifyPlanRejectsChangedSource(t *testing.T) {
	root := t.TempDir()
	name := "audio.m4a"
	path := filepath.Join(root, name)
	if err := os.WriteFile(path, []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	plan := validPlanForFile(t, root, name)
	if err := os.WriteFile(path, []byte("changed"), 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := DecodeAndVerifyPlan(encodePlan(t, plan), root)
	requireContractKind(t, err, "evidence_mismatch")
}

func TestDecodeAndVerifyPlanContractDoesNotOpenLiveSource(t *testing.T) {
	root := t.TempDir()
	name := "audio.m4a"
	path := filepath.Join(root, name)
	if err := os.WriteFile(path, []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	plan := validPlanForFile(t, root, name)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}

	verified, err := DecodeAndVerifyPlanContract(encodePlan(t, plan))
	if err != nil {
		t.Fatalf("closed contract verification should not open the live source: %v", err)
	}
	if verified.PlanSHA256 != plan.PlanSHA256 {
		t.Fatalf("unexpected plan digest: %s", verified.PlanSHA256)
	}
	if _, err := DecodeAndVerifyPlan(encodePlan(t, plan), root); err == nil {
		t.Fatal("full shadow verification must still reject the missing live source")
	}
}

func TestDecodeAndVerifyPlanRejectsDuplicateKeys(t *testing.T) {
	root := t.TempDir()
	raw := []byte(`{"schema_version":"x","schema_version":"y"}`)
	_, err := DecodeAndVerifyPlan(raw, root)
	requireContractKind(t, err, "invalid_plan")
}

func TestReadSourceEvidenceRejectsSymlinkAndHardlink(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target.m4a")
	if err := os.WriteFile(target, []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(root, "link.m4a")); err != nil {
		t.Fatal(err)
	}
	if _, err := readSourceEvidence(root, "link.m4a"); err == nil {
		t.Fatal("symlink source must be rejected")
	}

	if err := os.Link(target, filepath.Join(root, "hardlink.m4a")); err != nil {
		t.Fatal(err)
	}
	if _, err := readSourceEvidence(root, "target.m4a"); err == nil {
		t.Fatal("multi-link source must be rejected")
	}
}
