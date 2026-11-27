PYTHON ?= .venv/bin/python
export PYTHONPATH := $(CURDIR)/src

.PHONY: check check-python check-controller check-web
check: check-python check-controller check-web
check-python: python-test python-compile
check-controller: controller-contracts controller-race controller-sigterm controller-vet controller-darwin
check-web: web-test web-build

.PHONY: python-deps python-test python-compile
python-deps:
	"$(PYTHON)" -m pip install -r requirements.txt

python-test:
	"$(PYTHON)" -m unittest discover -s tests -p 'test_*.py' -v

python-compile:
	"$(PYTHON)" -m compileall -q scripts src tests

.PHONY: controller-contracts controller-race controller-sigterm controller-vet controller-darwin
controller-contracts:
	"$(PYTHON)" -m unittest \
		tests.test_controller_topology \
		tests.test_controller_bundle \
		tests.test_controller_activation \
		tests.test_controller_readiness \
		tests.test_controller_evidence \
		tests.test_shadow_probe \
		tests.test_single_job_contract \
		tests.test_retry_job_contract \
		tests.test_controller_gate \
		-v

controller-race:
	go -C controller test -race ./...

controller-sigterm:
	go -C controller test -race -count=10 -run '^(TestMainSIGTERMEmitsInterruptedReportAndReapsDescendants|TestExitedButUnreapedFixtureMemberIsNotLive)$$' ./cmd/lecture-stt-shadow

controller-vet:
	go -C controller vet ./...

# The Go test itself skips when macOS or the checkout's Python is missing.
controller-darwin:
	@test "$$(uname -s)" = Darwin || { echo 'controller-darwin requires macOS' >&2; exit 1; }
	@.venv/bin/python --version
	LECTURE_STT_RUN_DARWIN_ACCEPTANCE=1 go -C controller test ./internal/shadow -run TestRunDarwinAcceptance20ConsecutiveRuns -count=1 -v

.PHONY: web-deps web-test web-build
web-deps:
	cd frontend/web-panel && npm ci --no-audit --no-fund

web-test:
	cd frontend/web-panel && npm test

web-build:
	cd frontend/web-panel && npm run build
