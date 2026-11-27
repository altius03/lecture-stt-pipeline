//go:build darwin || linux

package main

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func privateDir(t *testing.T) string {
	t.Helper()
	base, err := filepath.EvalSymlinks(os.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp(base, "op-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func TestSocketAndTokenOwnership(t *testing.T) {
	dir := privateDir(t)
	token := filepath.Join(dir, "token")
	if err := os.WriteFile(token, []byte("fixture-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, err := readToken(token); err != nil || got != "fixture-token" {
		t.Fatalf("valid private credential rejected: %v", err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(token, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readToken(link); err == nil {
		t.Fatal("accepted linked credential")
	}
	hard := filepath.Join(dir, "hard")
	if err := os.Link(token, hard); err != nil {
		t.Fatal(err)
	}
	if _, err := readToken(token); err == nil {
		t.Fatal("accepted multi-link credential")
	}
	if err := os.Remove(hard); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(token, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readToken(token); err == nil {
		t.Fatal("accepted permissive credential")
	}
	if _, err := privateListener(link); err == nil {
		t.Fatal("replaced existing link at socket path")
	}
	socket := filepath.Join(dir, "panel.sock")
	listener, err := privateListener(socket)
	if err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(socket); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("incorrect socket permissions: %v %v", info, err)
	}
	if _, err := privateListener(socket); err == nil {
		t.Fatal("replaced an existing socket")
	}
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(socket); !os.IsNotExist(err) {
		t.Fatalf("socket remained after close: %v", err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := privateListener(socket); err == nil {
		t.Fatal("accepted non-private socket directory")
	}
}

func TestSocketRefusesSymlinkAndWritableParents(t *testing.T) {
	dir := privateDir(t)
	realParent := filepath.Join(dir, "private")
	if err := os.Mkdir(realParent, 0o700); err != nil {
		t.Fatal(err)
	}
	linkedParent := filepath.Join(dir, "alias")
	if err := os.Symlink(realParent, linkedParent); err != nil {
		t.Fatal(err)
	}
	if _, err := privateListener(filepath.Join(linkedParent, "x")); err == nil {
		t.Fatal("accepted a linked socket parent")
	}
	if err := os.Chmod(dir, 0o777); err != nil {
		t.Fatal(err)
	}
	if _, err := privateListener(filepath.Join(realParent, "x")); err == nil {
		t.Fatal("accepted an externally replaceable socket directory")
	}
}

func testProxy(t *testing.T, backendHandler http.Handler) (string, *http.Client, string) {
	t.Helper()
	upstream := httptest.NewServer(backendHandler)
	t.Cleanup(upstream.Close)
	dest, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(privateDir(t), "panel.sock")
	listener, err := privateListener(socket)
	if err != nil {
		t.Fatal(err)
	}
	proxy := &http.Server{Handler: operatorHandler(dest, "fixture-only-control-token")}
	go proxy.Serve(listener)
	t.Cleanup(func() { proxy.Close() })
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}
	t.Cleanup(transport.CloseIdleConnections)
	return "http://127.0.0.1:18765", &http.Client{Transport: transport, Timeout: 3 * time.Second}, upstream.URL
}

func TestBrowserBoundaryAndProtectedUpstream(t *testing.T) {
	requests := make(chan *http.Request, 10)
	base, client, upstream := testProxy(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture-only-control-token" {
			http.Error(w, "read only", 403)
			return
		}
		requests <- r.Clone(context.Background())
		w.WriteHeader(http.StatusAccepted)
	}))
	direct, err := http.Post(upstream+"/api/jobs/7/quality-review", "text/plain", nil)
	if err != nil {
		t.Fatal(err)
	}
	direct.Body.Close()
	if direct.StatusCode != http.StatusForbidden {
		t.Fatal("unauthenticated upstream accepted a write")
	}
	for _, tc := range []struct {
		method, host, origin, site string
		want                       int
	}{
		{"POST", "127.0.0.1:18765", base, "same-origin", 202},
		{"GET", "localhost:18765", "", "none", 202},
		{"POST", "127.0.0.1:18765", "", "", 403},
		{"POST", "127.0.0.1:18765", "https://elsewhere.invalid", "", 403},
		{"POST", "127.0.0.1:18765", base, "cross-site", 403},
		{"GET", "other.invalid:18765", "", "", 403},
		{"CONNECT", "127.0.0.1:18765", "", "", 405},
	} {
		t.Run(tc.method+"-"+tc.host+"-"+tc.origin+"-"+tc.site, func(t *testing.T) {
			req, err := http.NewRequest(tc.method, base+"/api/jobs/7/quality-review", nil)
			if err != nil {
				t.Fatal(err)
			}
			req.Host = tc.host
			for k, v := range map[string]string{
				"Authorization": "client-supplied", "X-Lecture-STT-Operator": "untrusted-user",
				"X-Forwarded-User": "untrusted-user", "Tailscale-User-Login": "untrusted-user",
				"Via": "untrusted-via", "Cookie": "unrelated-cookie",
			} {
				req.Header.Set(k, v)
			}
			if tc.origin != "" {
				req.Header.Set("Origin", tc.origin)
			}
			if tc.site != "" {
				req.Header.Set("Sec-Fetch-Site", tc.site)
			}
			res, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			res.Body.Close()
			if res.StatusCode != tc.want {
				t.Fatalf("want %d, got %d", tc.want, res.StatusCode)
			}
			if tc.want == 202 {
				seen := <-requests
				if "http://"+seen.Host != upstream || (tc.origin != "" && seen.Header.Get("Origin") != upstream) ||
					seen.Header.Get("X-Lecture-STT-Operator") != "ssh-owner" ||
					seen.Header.Get("Via") != "1.1 lecture-stt-operator" {
					t.Fatal("wrong upstream host, origin, or proxy operator")
				}
				for _, name := range []string{"X-Forwarded-User", "Tailscale-User-Login", "Cookie"} {
					if seen.Header.Get(name) != "" {
						t.Fatalf("client header forwarded: %s", name)
					}
				}
				if res.Header.Get("X-Frame-Options") != "DENY" {
					t.Fatal("missing framing boundary")
				}
			} else if len(requests) != 0 {
				t.Fatal("rejected request reached upstream")
			}
		})
	}
}

func TestStreamFlushAndRedirectBoundary(t *testing.T) {
	canceled := make(chan struct{})
	base, client, _ := testProxy(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirect" {
			http.Redirect(w, r, "https://example.invalid", http.StatusFound)
			return
		}
		if r.URL.RawQuery != "streams=state" {
			t.Error("query not preserved")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: ready\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(canceled)
	}))
	res, err := client.Get(base + "/api/events?streams=state")
	if err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(res.Body).ReadString('\n')
	res.Body.Close()
	if err != nil || line != "data: ready\n" {
		t.Fatalf("missing live event: %q, %v", line, err)
	}
	select {
	case <-canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("stream did not stop after browser disconnected")
	}
	res, err = client.Get(base + "/redirect")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 502 || res.Header.Get("Location") != "" || strings.Contains(string(body), "fixture-only") {
		t.Fatal("unsafe redirection or response leaked a credential")
	}
}
