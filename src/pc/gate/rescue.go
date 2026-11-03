package main

// Rescue after a kill switch (task WOL\Rescue = `wol-gate rescue` as SYSTEM, WOL\RescueUI = red
// full-screen page for the logged-on user). The owner types the recovery password (not the Windows
// one); then only the internet comes back, the PC asks the phone app, and the owner approves with
// a fingerprint. The PC checks that signature itself and only then brings SSH and the tunnel back.
//
//	wol-gate set-recovery     (admin) window to set the recovery password

import (
	"bytes"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

//go:embed rescue.html
var rescueHTML []byte

const (
	rescueAddr = "127.0.0.1:47811"
	rescueWait = 170 // seconds to approve on the phone (the PC accepts the signature for 3 min)
	passIter   = 600000
)

type Recovery struct {
	Salt  string `json:"salt"`
	Hash  string `json:"hash"`
	Iter  int    `json:"iter"`
	Fails int    `json:"fails"`
	Until int64  `json:"until"`
}

var rs = struct {
	sync.Mutex
	State, Msg string // setup | saved | nopass | locked | network | waiting | done
}{}

func setState(state, msg string) {
	rs.Lock()
	rs.State, rs.Msg = state, msg
	rs.Unlock()
}

func passHash(p string, salt []byte, iter int) []byte {
	k, _ := pbkdf2.Key(sha256.New, p, salt, iter, 32)
	return k
}

func psRun(script string) {
	if os.Getenv("WOLGATE_DIR") != "" { // tests never touch the real network cards
		return
	}
	exec.Command("powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedPS(script)).Run()
}

func rescueServe(setup bool) {
	logf("RESCUE: page on http://%s (setup=%v)", rescueAddr, setup)
	if err := http.ListenAndServe(rescueAddr, rescueMux(setup)); err != nil {
		logf("RESCUE: %v", err)
	}
}

func rescueMux(setup bool) http.Handler {
	var rec Recovery
	switch {
	case setup:
		setState("setup", "")
	case readJSON("recovery.json", &rec) != nil:
		setState("nopass", "")
	default:
		setState("locked", "")
	}
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, r *http.Request) bool {
		// loopback only, and no other web page may drive it (DNS rebinding / cross-site)
		if r.Host != rescueAddr || (r.Method == "POST" && r.Header.Get("X-Wolbot") != "1") {
			http.Error(w, "forbidden", 403)
			return false
		}
		return true
	}
	reply := func(w http.ResponseWriter) {
		rs.Lock()
		b, _ := json.Marshal(map[string]string{"state": rs.State, "msg": rs.Msg})
		rs.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.Write(b)
	}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if ok(w, r) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
			w.Write(rescueHTML)
		}
	})
	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		if ok(w, r) {
			reply(w)
		}
	})
	mux.HandleFunc("/api/setpass", func(w http.ResponseWriter, r *http.Request) {
		if !ok(w, r) || r.Method != "POST" || !setup {
			return
		}
		var b struct{ Password string }
		json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&b)
		if err := saveRecovery(b.Password); err != nil {
			setState("setup", err.Error())
			reply(w)
			return
		}
		setState("saved", "")
		reply(w)
		if os.Getenv("WOLGATE_DIR") == "" {
			go func() { time.Sleep(3 * time.Second); closeRescueWindows(); os.Exit(0) }()
		}
	})
	mux.HandleFunc("/api/pass", func(w http.ResponseWriter, r *http.Request) {
		if !ok(w, r) || r.Method != "POST" {
			return
		}
		rs.Lock()
		busy := rs.State != "locked"
		rs.Unlock()
		if busy {
			reply(w)
			return
		}
		var b struct{ Password string }
		json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&b)
		var rec Recovery
		if readJSON("recovery.json", &rec) != nil {
			setState("nopass", "")
			reply(w)
			return
		}
		now := time.Now().Unix()
		if rec.Until > now {
			setState("locked", fmt.Sprintf("too many wrong passwords - wait %d min / %d دقیقه صبر کن", (rec.Until-now)/60+1, (rec.Until-now)/60+1))
			reply(w)
			return
		}
		salt, _ := base64.StdEncoding.DecodeString(rec.Salt)
		want, _ := base64.StdEncoding.DecodeString(rec.Hash)
		if subtle.ConstantTimeCompare(passHash(b.Password, salt, rec.Iter), want) != 1 {
			rec.Fails++
			if rec.Fails >= 5 {
				rec.Until = now + int64(15*60)<<min(rec.Fails-5, 6)
			}
			writeJSON("recovery.json", &rec)
			logf("RESCUE: wrong recovery password (%d)", rec.Fails)
			setState("locked", "wrong password / رمز اشتباه است")
			reply(w)
			return
		}
		rec.Fails, rec.Until = 0, 0
		writeJSON("recovery.json", &rec)
		logf("RESCUE: recovery password accepted, asking the phone")
		setState("network", "")
		reply(w)
		if os.Getenv("WOLGATE_DIR") == "" {
			go rescueFlow()
		}
	})
	return mux
}

// password was right: bring the internet back, ask the phone, check its signature, restore everything
func rescueFlow() {
	psRun("Get-NetAdapter | Enable-NetAdapter -Confirm:$false")
	var st Store
	readJSON("gate-keys.json", &st)
	machine := machineName()
	ts := time.Now().Unix()
	nb := make([]byte, 18)
	rand.Read(nb)
	nonce := base64.RawURLEncoding.EncodeToString(nb)
	cl := http.Client{Timeout: 10 * time.Second}
	var id string
	for i := 0; i < 30 && id == ""; i++ { // DHCP may take a moment
		body, _ := json.Marshal(map[string]any{"machine": machine, "ts": ts, "nonce": nonce})
		req, _ := http.NewRequest("POST", st.Origin+"/api/rescue", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Wolbot", "1")
		if resp, err := cl.Do(req); err == nil {
			var j struct{ ID string }
			json.NewDecoder(resp.Body).Decode(&j)
			resp.Body.Close()
			id = j.ID
		}
		if id == "" {
			time.Sleep(3 * time.Second)
		}
	}
	if id == "" {
		rescueFail("could not reach the server")
		return
	}
	setState("waiting", "")
	for t0 := time.Now(); time.Since(t0) < rescueWait*time.Second; time.Sleep(3 * time.Second) {
		resp, err := cl.Get(st.Origin + "/api/rescue/" + id)
		if err != nil {
			continue
		}
		var j struct {
			Status string
			Cred   *Cred
		}
		json.NewDecoder(resp.Body).Decode(&j)
		resp.Body.Close()
		if j.Status != "approved" || j.Cred == nil {
			continue
		}
		r := &Req{Act: "unlock", TS: ts, Nonce: nonce, Cred: j.Cred}
		var s State
		readJSON("gate-state.json", &s)
		if s.Nonces == nil {
			s.Nonces = map[string]int64{}
		}
		k, err := verify(&st, &s, r)
		if err != nil {
			logf("RESCUE: phone approval refused: %v", err)
			rescueFail("the approval did not check out: " + err.Error())
			return
		}
		s.Fails = nil // a fresh start for the kill-switch counter
		writeJSON("gate-keys.json", &st)
		writeJSON("gate-state.json", &s)
		logf("RESCUE: unlocked, approved by %s (%s)", k.Name, k.Owner)
		psRun(`Set-Service sshd -StartupType Automatic; Start-Service sshd
schtasks /change /tn "WOL\Tunnel" /enable | Out-Null
schtasks /run /tn "WOL\Tunnel" | Out-Null`)
		setState("done", "")
		time.Sleep(6 * time.Second)
		rescueClose()
		os.Exit(0)
	}
	rescueFail("no approval from the phone in time")
}

func rescueFail(why string) {
	logf("RESCUE: %s - internet off again", why)
	psRun("Get-NetAdapter | Where-Object Status -ne Disabled | Disable-NetAdapter -Confirm:$false")
	setState("locked", why+" - try again / دوباره امتحان کن")
}

// close the red screen and turn the rescue tasks off until the next kill switch
func rescueClose() {
	run("schtasks", "/change", "/tn", `WOL\RescueUI`, "/disable")
	run("schtasks", "/change", "/tn", `WOL\Rescue`, "/disable")
	closeRescueWindows()
}

// the Edge window of the rescue / set-recovery page (its own profile folder WOLRescue)
func closeRescueWindows() {
	psRun(`Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -match 'WOLRescue' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`)
}

func edgePath() string {
	for _, p := range []string{
		filepath.Join(os.Getenv("ProgramFiles(x86)"), `Microsoft\Edge\Application\msedge.exe`),
		filepath.Join(os.Getenv("ProgramFiles"), `Microsoft\Edge\Application\msedge.exe`),
	} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// the window (red full screen for the rescue, a normal window for set-recovery)
func rescueUI(kiosk bool) error {
	cl := http.Client{Timeout: 2 * time.Second}
	for i := 0; i < 60; i++ { // wait for the SYSTEM task to listen
		req, _ := http.NewRequest("GET", "http://"+rescueAddr+"/api/status", nil)
		if resp, err := cl.Do(req); err == nil {
			resp.Body.Close()
			break
		}
		time.Sleep(time.Second)
	}
	edge := edgePath()
	if edge == "" {
		return errors.New("Microsoft Edge not found")
	}
	prof := filepath.Join(os.Getenv("LOCALAPPDATA"), "WOLRescue")
	args := []string{"--user-data-dir=" + prof, "--no-first-run"}
	if kiosk {
		args = append(args, "--kiosk", "http://"+rescueAddr+"/", "--edge-kiosk-type=fullscreen")
	} else {
		args = append(args, "--app=http://"+rescueAddr+"/", "--window-size=760,640")
	}
	return exec.Command(edge, args...).Start()
}

func saveRecovery(p string) error {
	if len([]rune(p)) < 8 {
		return errors.New("at least 8 characters / حداقل ۸ حرف")
	}
	salt := make([]byte, 16)
	rand.Read(salt)
	rec := Recovery{Salt: base64.StdEncoding.EncodeToString(salt), Iter: passIter,
		Hash: base64.StdEncoding.EncodeToString(passHash(p, salt, passIter))}
	if err := writeJSON("recovery.json", &rec); err != nil {
		return errors.New("cannot save (run as administrator): " + err.Error())
	}
	lockACL("recovery.json")
	logf("RESCUE: recovery password set")
	return nil
}

// set-recovery --stdin: WOL Agent sends the password on stdin (never on the command line)
func setRecoveryStdin() {
	b, _ := io.ReadAll(io.LimitReader(os.Stdin, 4096))
	if err := saveRecovery(strings.TrimRight(string(b), "\r\n")); err != nil {
		fmt.Println("recovery password:", err)
		os.Exit(1)
	}
	fmt.Println("recovery password saved")
}

func setRecovery() {
	go func() {
		time.Sleep(time.Second)
		if err := rescueUI(false); err != nil {
			fmt.Println("open http://" + rescueAddr + "/ in a browser (" + err.Error() + ")")
		}
	}()
	fmt.Println("A window opened: type the recovery password twice and press Save.")
	rescueServe(true)
}
