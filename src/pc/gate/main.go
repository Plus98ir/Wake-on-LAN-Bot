// wol-gate - the PC's own gatekeeper for wolbot.
//
// The wolbot server reaches this PC only through this program: its key in
// administrators_authorized_keys carries command="...\wol-gate.exe". Every action that changes
// something must carry a fresh passkey (fingerprint) signature over exactly that action on
// exactly this PC, checked here against keys the owner trusted on this PC. A hacked server can
// not run anything; it can only wake the PC, read harmless status or cut the PC off.
//
//	wol-gate                          (forced command) one request as JSON on stdin
//	wol-gate trust <https://auth-site> [--yes name,name]   pick which passkeys this PC trusts
//	wol-gate list | untrust <name> | log
package main

import (
	"bufio"
	"bytes"
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	_ "embed"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf16"
)

//go:embed kill.ps1
var killPS string

const (
	maxAge    = 180 // seconds a signed request stays valid
	failLimit = 3   // refused requests within failWindow start the kill switch
	failWin   = 600
)

// actions that need a signature, and the ones a server may ask for without one
var signed = map[string]bool{"lock": true, "restart": true, "shutdown": true, "who": true, "app": true, "claude": true, "claude_stop": true}
var open = map[string]bool{"ping": true, "apps": true, "ip": true, "cs": true, "kill": true}

type Key struct {
	ID    string   `json:"id"`
	Pub   string   `json:"pub"`
	Alg   int      `json:"alg"`
	Name  string   `json:"name"`
	Owner string   `json:"owner"`
	FP    string   `json:"fp"`
	Allow []string `json:"allow"`
	Count uint32   `json:"count"`
}

type Store struct {
	Origin string `json:"origin"`
	Keys   []Key  `json:"keys"`
}

type State struct {
	Nonces map[string]int64 `json:"nonces"`
	Fails  []int64          `json:"fails"`
}

type Cred struct {
	ID                string `json:"id"`
	ClientDataJSON    string `json:"clientDataJSON"`
	AuthenticatorData string `json:"authenticatorData"`
	Signature         string `json:"signature"`
}

type Req struct {
	Act   string `json:"act"`
	Arg   string `json:"arg"`
	TS    int64  `json:"ts"`
	Nonce string `json:"nonce"`
	Cred  *Cred  `json:"cred"`
}

var dir = func() string {
	if d := os.Getenv("WOLGATE_DIR"); d != "" { // tests only
		return d
	}
	return filepath.Join(os.Getenv("ProgramData"), "wol-agent")
}()

func path(n string) string { return filepath.Join(dir, n) }

func readJSON(name string, v any) error {
	b, err := os.ReadFile(path(name))
	if err != nil {
		return err
	}
	return json.Unmarshal(bytes.TrimPrefix(b, []byte("\xef\xbb\xbf")), v)
}

func writeJSON(name string, v any) error {
	b, _ := json.MarshalIndent(v, "", "  ")
	tmp := path(name + ".tmp")
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path(name))
}

func logf(format string, a ...any) {
	f, err := os.OpenFile(path("gate.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s %s\n", time.Now().Format("2006-01-02 15:04:05"), fmt.Sprintf(format, a...))
}

// tell whoever sits at the PC (message box on every session)
func tellDesk(text string) {
	if os.Getenv("WOLGATE_DIR") != "" { // tests: no message boxes
		return
	}
	exec.Command("msg", "*", "/time:120", "wolbot: "+text).Run()
}

func b64d(s string) ([]byte, error) { return base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "=")) }

func machineName() string {
	var a struct {
		Name string `json:"name"`
	}
	readJSON("agent.json", &a)
	return a.Name
}

// Challenge the phone must have signed: binds the signature to this action on this PC.
func challenge(machine string, r *Req) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("wolbot1|%s|%s|%s|%d|%s", machine, r.Act, r.Arg, r.TS, r.Nonce)))
	return base64.RawURLEncoding.EncodeToString(h[:])
}

func verify(st *Store, s *State, r *Req) (*Key, error) {
	c := r.Cred
	if c == nil {
		return nil, errors.New("no signature")
	}
	var k *Key
	for i := range st.Keys {
		if st.Keys[i].ID == c.ID {
			k = &st.Keys[i]
		}
	}
	if k == nil {
		return nil, errors.New("passkey not trusted on this PC")
	}
	now := time.Now().Unix()
	if r.TS < now-maxAge || r.TS > now+60 {
		return k, errors.New("request too old")
	}
	if len(r.Nonce) < 16 || s.Nonces[r.Nonce] != 0 {
		return k, errors.New("replayed request")
	}
	cdRaw, err := b64d(c.ClientDataJSON)
	if err != nil {
		return k, err
	}
	var cd struct{ Type, Challenge, Origin string }
	if err := json.Unmarshal(cdRaw, &cd); err != nil {
		return k, err
	}
	if cd.Type != "webauthn.get" || cd.Challenge != challenge(machineName(), r) || cd.Origin != st.Origin {
		return k, errors.New("signature is not for this action on this PC")
	}
	ad, err := b64d(c.AuthenticatorData)
	if err != nil || len(ad) < 37 {
		return k, errors.New("bad authenticator data")
	}
	u, _ := url.Parse(st.Origin)
	rp := sha256.Sum256([]byte(u.Hostname()))
	if !bytes.Equal(ad[:32], rp[:]) || ad[32]&0x01 == 0 || ad[32]&0x04 == 0 {
		return k, errors.New("wrong site or no fingerprint")
	}
	count := binary.BigEndian.Uint32(ad[33:37])
	if count != 0 && count <= k.Count {
		return k, errors.New("sign counter went back (cloned key?)")
	}
	der, err := b64d(k.Pub)
	if err != nil {
		return k, err
	}
	pub, err := x509.ParsePKIXPublicKey(der)
	if err != nil {
		return k, err
	}
	sig, err := b64d(c.Signature)
	if err != nil {
		return k, err
	}
	cdh := sha256.Sum256(cdRaw)
	msg := append(append([]byte{}, ad...), cdh[:]...)
	digest := sha256.Sum256(msg)
	ok := false
	switch p := pub.(type) {
	case *ecdsa.PublicKey:
		ok = ecdsa.VerifyASN1(p, digest[:], sig)
	case *rsa.PublicKey:
		ok = rsa.VerifyPKCS1v15(p, crypto.SHA256, digest[:], sig) == nil
	case ed25519.PublicKey:
		ok = ed25519.Verify(p, msg, sig)
	}
	if !ok {
		return k, errors.New("bad signature")
	}
	allowed := false
	for _, a := range k.Allow {
		allowed = allowed || a == "all" || a == r.Act
	}
	if !allowed {
		return k, errors.New("this passkey may not do " + r.Act + " on this PC")
	}
	k.Count = count
	s.Nonces[r.Nonce] = now
	return k, nil
}

func run(name string, args ...string) (string, error) {
	out, err := exec.Command(name, args...).CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

func encodedPS(script string) string {
	u := utf16.Encode([]rune(script))
	b := make([]byte, len(u)*2)
	for i, v := range u {
		binary.LittleEndian.PutUint16(b[i*2:], v)
	}
	return base64.StdEncoding.EncodeToString(b)
}

func kill(reason string) {
	logf("KILL SWITCH: %s", reason)
	tellDesk("KILL SWITCH - " + reason)
	run("powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedPS(killPS))
}

var taskName = regexp.MustCompile(`^[A-Za-z0-9]{1,24}$`)

func act(r *Req) (string, error) {
	claude := []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path("wol-claude.ps1")}
	switch r.Act {
	case "ping":
		return "pong " + machineName(), nil
	case "apps": // button names only: no paths or other details of this PC
		var list []map[string]any
		var one map[string]any
		b, err := os.ReadFile(path("apps.json"))
		b = bytes.TrimPrefix(b, []byte("\xef\xbb\xbf"))
		if json.Unmarshal(b, &list) != nil && json.Unmarshal(b, &one) == nil {
			list = []map[string]any{one}
		}
		var out []map[string]string
		for _, x := range list {
			out = append(out, map[string]string{"name": fmt.Sprint(x["name"]), "task": fmt.Sprint(x["task"])})
		}
		j, _ := json.Marshal(out)
		return string(j), err
	case "ip":
		var a struct {
			Server string `json:"server"`
		}
		readJSON("agent.json", &a)
		if !regexp.MustCompile(`^\d{2,5}$`).MatchString(r.Arg) || a.Server == "" {
			return "", errors.New("bad ip request")
		}
		cl := http.Client{Timeout: 8 * time.Second}
		resp, err := cl.Get("http://" + a.Server + ":" + r.Arg + "/ip")
		if err != nil {
			return "", err
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 64))
		return string(b), nil
	case "cs": // only running / not running: the session link stays on this PC
		out, _ := run("powershell", append(claude, "-Status")...)
		if strings.HasPrefix(out, "running") {
			return "running - open the Claude app -> Code: the newest session is this PC", nil
		}
		return "not running", nil
	case "kill":
		// only registers and starts task WOL\Kill (SYSTEM), which goes on after this session is cut
		kill("asked by the wolbot server")
		return "kill switch started", nil
	case "lock":
		return run("schtasks", "/run", "/tn", `WOL\Lock`)
	case "restart":
		return run("shutdown", "/r", "/t", "5", "/f")
	case "shutdown":
		return run("shutdown", "/s", "/t", "5", "/f")
	case "who":
		out, _ := run("query", "user") // rc 1 even when it works
		return out, nil
	case "app":
		if !taskName.MatchString(r.Arg) {
			return "", errors.New("bad app name")
		}
		return run("schtasks", "/run", "/tn", `WOL\`+r.Arg)
	case "claude":
		return run("schtasks", "/run", "/tn", `WOL\Claude`)
	case "claude_stop":
		return run("powershell", append(claude, "-Stop")...)
	}
	return "", errors.New("unknown action")
}

func serve() int {
	var r Req
	if err := json.NewDecoder(io.LimitReader(os.Stdin, 64<<10)).Decode(&r); err != nil {
		logf("REFUSED: not a gate request (%v) - someone tried a shell or a plain command", err)
		tellDesk("refused a connection that was not a signed request")
		fmt.Println("refused: this PC only accepts signed wolbot requests")
		return 3
	}
	if open[r.Act] {
		if r.Act != "ping" && r.Act != "ip" && r.Act != "apps" && r.Act != "cs" {
			logf("open action %s", r.Act)
		}
		out, err := act(&r)
		fmt.Println(out)
		if err != nil {
			fmt.Println(err)
			return 1
		}
		return 0
	}
	if !signed[r.Act] {
		logf("REFUSED: unknown action %q", r.Act)
		fmt.Println("refused: unknown action")
		return 3
	}
	var st Store
	readJSON("gate-keys.json", &st)
	var s State
	readJSON("gate-state.json", &s)
	if s.Nonces == nil {
		s.Nonces = map[string]int64{}
	}
	now := time.Now().Unix()
	for n, t := range s.Nonces {
		if t < now-maxAge-120 {
			delete(s.Nonces, n)
		}
	}
	k, err := verify(&st, &s, &r)
	if err != nil {
		var fails []int64
		for _, t := range s.Fails {
			if t > now-failWin {
				fails = append(fails, t)
			}
		}
		s.Fails = append(fails, now)
		writeJSON("gate-state.json", &s)
		who := "unknown key"
		if k != nil {
			who = k.Name
		}
		logf("REFUSED %s %s (%s): %v [%d/%d]", r.Act, r.Arg, who, err, len(s.Fails), failLimit)
		tellDesk(fmt.Sprintf("REFUSED %s (%s): %v", r.Act, who, err))
		fmt.Println("refused:", err)
		if len(s.Fails) >= failLimit {
			kill(fmt.Sprintf("%d refused requests in %d min", len(s.Fails), failWin/60))
		}
		return 3
	}
	writeJSON("gate-keys.json", &st) // new sign counter
	writeJSON("gate-state.json", &s)
	logf("OK %s %s by %s (%s)", r.Act, r.Arg, k.Name, k.Owner)
	if r.Act != "who" && r.Act != "lock" {
		tellDesk(fmt.Sprintf("%s %s approved by fingerprint of %s", r.Act, r.Arg, k.Name))
	}
	out, err := act(&r)
	fmt.Println(out)
	if err != nil {
		fmt.Println(err)
		return 1
	}
	return 0
}

// ---------- owner commands (run at the PC, as administrator) ----------

func lockACL(name string) {
	run("icacls", path(name), "/inheritance:r", "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F")
}

func trust(site string, yes []string) error {
	site = strings.TrimRight(site, "/")
	if !strings.HasPrefix(site, "https://") {
		return errors.New("the site must start with https://")
	}
	cl := http.Client{Timeout: 15 * time.Second}
	resp, err := cl.Get(site + "/api/pubkeys")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var keys []Key
	if err := json.NewDecoder(resp.Body).Decode(&keys); err != nil {
		return err
	}
	var st Store
	readJSON("gate-keys.json", &st)
	st.Origin = site
	in := bufio.NewReader(os.Stdin)
	var kept []Key
	for _, k := range keys {
		der, _ := b64d(k.Pub)
		h := sha256.Sum256(der)
		k.FP = hex.EncodeToString(h[:6])
		take := false
		if yes != nil {
			for _, n := range yes {
				take = take || n == k.Name
			}
			k.Allow = []string{"all"}
		} else {
			fmt.Printf("\nPasskey %q  owner %s  fingerprint %s\nTrust it on this PC? [y/N] ", k.Name, k.Owner, k.FP)
			a, _ := in.ReadString('\n')
			take = strings.EqualFold(strings.TrimSpace(a), "y")
			if take {
				fmt.Print("Allowed actions (Enter = all; or e.g. lock,who,app): ")
				a, _ = in.ReadString('\n')
				k.Allow = []string{"all"}
				if a = strings.TrimSpace(a); a != "" {
					k.Allow = strings.Split(a, ",")
				}
			}
		}
		if take {
			for _, old := range st.Keys { // keep the counter of a key trusted before
				if old.ID == k.ID {
					k.Count = old.Count
				}
			}
			kept = append(kept, k)
		}
	}
	st.Keys = kept
	if err := writeJSON("gate-keys.json", &st); err != nil {
		return err
	}
	lockACL("gate-keys.json")
	logf("TRUST: %d key(s) trusted for %s", len(kept), site)
	fmt.Printf("\nThis PC now trusts %d passkey(s) from %s.\n", len(kept), site)
	return nil
}

func main() {
	if len(os.Args) < 2 {
		os.Exit(serve())
	}
	var st Store
	readJSON("gate-keys.json", &st)
	switch os.Args[1] {
	case "trust":
		if len(os.Args) < 3 {
			fmt.Println("wol-gate trust <https://auth-site> [--yes name,name]")
			os.Exit(2)
		}
		var yes []string
		if len(os.Args) > 4 && os.Args[3] == "--yes" {
			yes = strings.Split(os.Args[4], ",")
		}
		if err := trust(os.Args[2], yes); err != nil {
			fmt.Println("error:", err)
			os.Exit(1)
		}
	case "list":
		fmt.Println("site:", st.Origin)
		for _, k := range st.Keys {
			fmt.Printf("%-16s owner %-12s fingerprint %s  allow %s\n", k.Name, k.Owner, k.FP, strings.Join(k.Allow, ","))
		}
	case "untrust":
		var kept []Key
		for _, k := range st.Keys {
			if len(os.Args) > 2 && k.Name != os.Args[2] {
				kept = append(kept, k)
			}
		}
		st.Keys = kept
		writeJSON("gate-keys.json", &st)
		logf("UNTRUST %v", os.Args[2:])
		fmt.Println("done")
	case "log":
		b, _ := os.ReadFile(path("gate.log"))
		os.Stdout.Write(b)
	default:
		fmt.Println("wol-gate trust <https://auth-site> [--yes name,name] | list | untrust <name> | log")
		os.Exit(2)
	}
}
