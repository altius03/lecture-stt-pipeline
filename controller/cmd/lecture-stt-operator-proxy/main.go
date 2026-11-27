//go:build darwin || linux

// The SSH owner forwards a local browser port to this private Unix socket.
// The shared panel's token stays on the server.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const backend = "127.0.0.1:8765"

// A private leaf is safe only if another local account cannot rename an
// ancestor. System sticky temporary directories can safely contain owned dirs.
func validateParent(path string, private bool) error {
	if !filepath.IsAbs(path) || path != filepath.Clean(path) {
		return errors.New("expected a canonical absolute path")
	}
	parent := filepath.Dir(path)
	for dir := parent; ; dir = filepath.Dir(dir) {
		info, err := os.Lstat(dir)
		if err != nil || !info.IsDir() {
			return errors.New("parent directories must exist without symlinks")
		}
		owner := int(info.Sys().(*syscall.Stat_t).Uid)
		if private && dir == parent && (owner != os.Geteuid() || info.Mode().Perm() != 0o700) {
			return errors.New("socket parent must be an owner-owned 0700 directory")
		}
		if owner != os.Geteuid() && owner != 0 {
			return errors.New("untrusted directory owner")
		}
		if info.Mode().Perm()&0o022 != 0 && info.Mode()&os.ModeSticky == 0 {
			return errors.New("parent directory is writable by another account")
		}
		if filepath.Dir(dir) == dir {
			return nil
		}
	}
}

func readToken(path string) (string, error) {
	if err := validateParent(path, false); err != nil {
		return "", err
	}
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err != nil {
		return "", errors.New("cannot open the private token file")
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	st := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || int(st.Uid) != os.Geteuid() || st.Nlink != 1 {
		return "", errors.New("token-file must be a single-link, owner-owned 0600 regular file")
	}
	const maxBytes = 4096
	data, err := io.ReadAll(io.LimitReader(file, maxBytes+1))
	if err != nil || len(data) > maxBytes {
		return "", errors.New("invalid token file")
	}
	token := strings.TrimSuffix(string(data), "\n")
	if token == "" || strings.IndexFunc(token, func(r rune) bool { return r < 33 || r > 126 }) >= 0 {
		return "", errors.New("token-file must contain exactly one nonempty ASCII token")
	}
	return token, nil
}

func privateListener(path string) (*net.UnixListener, error) {
	if err := validateParent(path, true); err != nil {
		return nil, err
	}
	if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		return nil, errors.New("socket path already exists or is inaccessible")
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0o600); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

func operatorHandler(upstream *url.URL, token string) http.Handler {
	proxy := &httputil.ReverseProxy{
		Transport: &http.Transport{
			DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
			ResponseHeaderTimeout: 30 * time.Second,
			IdleConnTimeout:       30 * time.Second,
		},
		FlushInterval: -1, // Deliver SSE as each frame arrives.
		Rewrite: func(req *httputil.ProxyRequest) {
			req.SetURL(upstream)
			for header := range req.Out.Header {
				key := strings.ToLower(header)
				if key == "via" || key == "forwarded" || key == "x-real-ip" || key == "authorization" ||
					key == "proxy-authorization" || key == "cookie" || strings.HasPrefix(key, "x-forwarded-") ||
					strings.HasPrefix(key, "tailscale-") || strings.HasPrefix(key, "x-lecture-stt-") {
					req.Out.Header.Del(header)
				}
			}
			req.Out.Header.Set("Via", "1.1 lecture-stt-operator")
			req.Out.Header.Set("Authorization", "Bearer "+token)
			req.Out.Header.Set("X-Lecture-STT-Operator", "ssh-owner")
			if req.In.Header.Get("Origin") != "" {
				req.Out.Header.Set("Origin", upstream.Scheme+"://"+upstream.Host)
			}
		},
		ModifyResponse: func(res *http.Response) error {
			if location := res.Header.Get("Location"); location != "" {
				dest, err := url.Parse(location)
				if err != nil || dest.IsAbs() || dest.Host != "" || strings.HasPrefix(location, "//") || strings.Contains(location, "\\") {
					return errors.New("external panel redirect refused")
				}
			}
			res.Header.Set("X-Frame-Options", "DENY")
			res.Header.Add("Content-Security-Policy", "frame-ancestors 'none'")
			res.Header.Set("Referrer-Policy", "same-origin")
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "panel unavailable", http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if (r.Host != "127.0.0.1:18765" && r.Host != "localhost:18765") ||
			r.URL.IsAbs() || r.URL.Host != "" || !strings.HasPrefix(r.URL.Path, "/") ||
			strings.HasPrefix(r.URL.Path, "//") || strings.Contains(r.URL.Path, "\\") || r.Header.Get("Upgrade") != "" {
			http.Error(w, "untrusted browser address", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodPost {
			w.Header().Set("Allow", "GET, HEAD, POST")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		origins := r.Header.Values("Origin")
		sites := r.Header.Values("Sec-Fetch-Site")
		if len(origins) > 1 || (len(origins) == 1 && origins[0] != "http://"+r.Host) ||
			(r.Method == http.MethodPost && len(origins) != 1) ||
			len(sites) > 1 || (len(sites) == 1 && sites[0] != "same-origin" && sites[0] != "none") {
			http.Error(w, "cross-origin request refused", http.StatusForbidden)
			return
		}
		proxy.ServeHTTP(w, r)
	})
}

func run() error {
	socket := flag.String("socket", "", "absolute private Unix socket path below an owner-owned 0700 directory")
	tokenFile := flag.String("token-file", "", "absolute 0600 file containing the panel's control token")
	closeOnEOF := flag.Bool("exit-on-stdin-close", false, "exit when the controlling SSH session closes stdin")
	flag.Parse()
	if *socket == "" || *tokenFile == "" || flag.NArg() != 0 {
		return errors.New("expected --socket PATH --token-file PATH; use --help for details")
	}
	token, err := readToken(*tokenFile)
	if err != nil {
		return err
	}
	listener, err := privateListener(*socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGHUP, syscall.SIGTERM)
	defer cancel()
	if *closeOnEOF {
		go func() {
			_, _ = io.Copy(io.Discard, os.Stdin)
			cancel()
		}()
	}
	upstream := &url.URL{Scheme: "http", Host: backend}
	server := &http.Server{
		Handler:           operatorHandler(upstream, token),
		BaseContext:       func(net.Listener) context.Context { return ctx },
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() { <-ctx.Done(); _ = server.Close() }()
	log.Print("operator socket ready; open http://127.0.0.1:18765 through the SSH forward")
	err = server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
