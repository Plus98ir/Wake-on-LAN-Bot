// WOL Agent - pairs a Windows PC with wolbot: detects the system, runs setup-pc.ps1, shows the result.
// UI = local page in an Edge app window; the program exits when that window closes.
package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

//go:embed ui.html
var uiHTML []byte

//go:embed setup-pc.ps1
var setupPS []byte

//go:embed icon.png
var iconPNG []byte

const createNoWindow = 0x08000000

var (
	lastPing = time.Now()
	pingMu   sync.Mutex
	runMu    sync.Mutex
	script   string
)

func main() {
	if !isAdmin() {
		relaunchElevated()
		return
	}
	dir := filepath.Join(os.Getenv("ProgramData"), "wol-agent")
	_ = os.MkdirAll(dir, 0o755)
	script = filepath.Join(os.TempDir(), "wol-agent-setup.ps1")
	// UTF-8 BOM so Windows PowerShell 5.1 reads the script as UTF-8
	_ = os.WriteFile(script, append([]byte{0xEF, 0xBB, 0xBF}, setupPS...), 0o644)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		msgBox("WOL Agent", "Cannot open a local port: "+err.Error())
		return
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(uiHTML)
	})
	mux.HandleFunc("/icon.png", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		w.Write(iconPNG)
	})
	mux.HandleFunc("/api/ping", func(w http.ResponseWriter, r *http.Request) {
		pingMu.Lock()
		lastPing = time.Now()
		pingMu.Unlock()
		w.WriteHeader(204)
	})
	mux.HandleFunc("/api/detect", func(w http.ResponseWriter, r *http.Request) {
		out, err := ps("-Detect").Output()
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(strings.TrimSpace(string(out))))
	})
	mux.HandleFunc("/api/run", runHandler)
	mux.HandleFunc("/api/quit", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(204)
		go func() { time.Sleep(200 * time.Millisecond); os.Exit(0) }()
	})
	go http.Serve(ln, mux)

	openWindow("http://" + ln.Addr().String() + "/")

	// exit when the window stops pinging (closed); first ping may take a while
	for {
		time.Sleep(3 * time.Second)
		pingMu.Lock()
		idle := time.Since(lastPing)
		pingMu.Unlock()
		if idle > 20*time.Second && !running() {
			os.Exit(0)
		}
	}
}

func running() bool {
	if runMu.TryLock() {
		runMu.Unlock()
		return false
	}
	return true
}

func ps(args ...string) *exec.Cmd {
	a := append([]string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script}, args...)
	c := exec.Command("powershell.exe", a...)
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	return c
}

type runReq struct {
	Action    string `json:"action"` // install | uninstall
	Code      string `json:"code"`
	Name      string `json:"name"`
	WakeHost  string `json:"wakeHost"`
	ExtraKey  string `json:"extraKey"`
	ClaudeDir string `json:"claudeDir"`
	NoLock    bool   `json:"noLock"`
}

// runHandler streams the script output to the page as plain text.
func runHandler(w http.ResponseWriter, r *http.Request) {
	var req runReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	if !runMu.TryLock() {
		http.Error(w, "already running", 409)
		return
	}
	defer runMu.Unlock()

	var args []string
	switch req.Action {
	case "uninstall":
		args = []string{"-Uninstall"}
	case "install":
		args = []string{"-PairCode", req.Code}
		if req.Name != "" {
			args = append(args, "-Name", req.Name)
		}
		if req.WakeHost != "" {
			args = append(args, "-WakeHost", req.WakeHost)
		}
		if strings.TrimSpace(req.ExtraKey) != "" {
			args = append(args, "-ExtraKey", strings.TrimSpace(req.ExtraKey))
		}
		if strings.TrimSpace(req.ClaudeDir) != "" {
			args = append(args, "-ClaudeDir", strings.TrimSpace(req.ClaudeDir))
		}
		if req.NoLock {
			args = append(args, "-NoLockAtLogon")
		}
	default:
		http.Error(w, "bad action", 400)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	fl, _ := w.(http.Flusher)
	c := ps(args...)
	pr, pw := io.Pipe()
	c.Stdout, c.Stderr = pw, pw
	if err := c.Start(); err != nil {
		fmt.Fprintln(w, "ERROR:", err)
		return
	}
	go func() { c.Wait(); pw.Close() }()
	buf := make([]byte, 4096)
	for {
		n, err := pr.Read(buf)
		if n > 0 {
			w.Write(buf[:n])
			if fl != nil {
				fl.Flush()
			}
		}
		if err != nil {
			break
		}
	}
	if c.ProcessState != nil && c.ProcessState.ExitCode() != 0 {
		fmt.Fprintf(w, "\n__EXIT__ %d\n", c.ProcessState.ExitCode())
	} else {
		fmt.Fprint(w, "\n__EXIT__ 0\n")
	}
}

// openWindow uses Edge app mode (own profile, so the window is ours); falls back to the default browser.
func openWindow(url string) {
	for _, p := range []string{
		filepath.Join(os.Getenv("ProgramFiles(x86)"), `Microsoft\Edge\Application\msedge.exe`),
		filepath.Join(os.Getenv("ProgramFiles"), `Microsoft\Edge\Application\msedge.exe`),
	} {
		if _, err := os.Stat(p); err == nil {
			prof := filepath.Join(os.Getenv("LOCALAPPDATA"), "WOLAgent", "edge")
			c := exec.Command(p, "--app="+url, "--user-data-dir="+prof, "--window-size=1200,860", "--no-first-run")
			if c.Start() == nil {
				return
			}
		}
	}
	exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
}

// ---------- Windows helpers (no external deps) ----------

var (
	shell32 = syscall.NewLazyDLL("shell32.dll")
	user32  = syscall.NewLazyDLL("user32.dll")
)

func isAdmin() bool {
	r, _, _ := shell32.NewProc("IsUserAnAdmin").Call()
	return r != 0
}

func relaunchElevated() {
	exe, _ := os.Executable()
	verb, _ := syscall.UTF16PtrFromString("runas")
	file, _ := syscall.UTF16PtrFromString(exe)
	args, _ := syscall.UTF16PtrFromString(strings.Join(os.Args[1:], " "))
	shell32.NewProc("ShellExecuteW").Call(0, uintptr(unsafe.Pointer(verb)), uintptr(unsafe.Pointer(file)),
		uintptr(unsafe.Pointer(args)), 0, 1)
}

func msgBox(title, text string) {
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(text)
	user32.NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), 0x10)
}
