package main

// Watchdog (`wol-gate watch`, task WOL\Watch as SYSTEM): every 15 s it compares a snapshot of what
// decides who can get into this PC with the approved one. A change locks the screen and waits for
// the owner: approve = a passkey signature over that incident, checked here (act "approve");
// deny (act "deny") or no answer to a dangerous change within 90 s = kill switch. A lock-only change
// (service, task) locks once and waits in the app, with no deadline, until the owner answers or undoes it.

import (
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"
)

//go:embed watch.ps1
var watchPS string

//go:embed neutralize.ps1
var neutralizePS string

const (
	watchEvery   = 15 * time.Second
	answerWithin = 90 // seconds to approve a dangerous change before the full network kill
)

// changes that open a way into the PC: no answer = kill switch. The rest only stay locked + logged.
var dangerous = map[string]bool{"admins": true, "users": true, "firewall": true, "uac": true, "rdp": true,
	"keys": true, "trusted": true, "remote": true, "failed": true}

// dangerous kinds we can put straight back to the approved state the instant we see them, so the open
// door is shut within one tick (the full network kill still follows if no one approves). keys/trusted
// can't be rebuilt from a snapshot and failed is an event, so those fall through to lock + kill only.
var neutralizable = map[string]bool{"rdp": true, "uac": true, "firewall": true, "admins": true, "users": true, "remote": true}

type Incident struct {
	ID     string `json:"id"`
	Kind   string `json:"kind"`
	Detail string `json:"detail"`
	TS     int64  `json:"ts"`
	Status string `json:"status"` // pending | approved | denied | timeout | reverted | superseded
	// Items: what the new thing is (file, signature), shown to the owner. Snap: the state that was shown,
	// and the only one an approval accepts: whatever appears after it needs its own incident.
	Items []ChangeItem `json:"items,omitempty"`
	Snap  []string     `json:"snap,omitempty"`
}

type Watch struct {
	Base  map[string][]string `json:"base"`
	Inc   []Incident          `json:"incidents"`
	Alive int64               `json:"alive"`
}

// one writer at a time (the watch loop and gate requests)
func withWatch(f func(w *Watch)) {
	lock := path("watch.lock")
	for i := 0; i < 100; i++ {
		if os.Mkdir(lock, 0o700) == nil {
			break
		}
		if st, err := os.Stat(lock); err == nil && time.Since(st.ModTime()) > 30*time.Second {
			os.Remove(lock)
		}
		time.Sleep(100 * time.Millisecond)
	}
	defer os.Remove(lock)
	var w Watch
	readJSON("watch-state.json", &w)
	f(&w)
	writeJSON("watch-state.json", &w)
}

func snapshot() (map[string][]string, error) {
	// stdout only: PowerShell writes progress records ("#< CLIXML") to stderr
	b, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedPS(watchPS)).Output()
	out := strings.TrimSpace(string(b))
	if err != nil {
		return nil, fmt.Errorf("snapshot: %v %s", err, out)
	}
	raw := map[string]any{}
	if err := json.Unmarshal([]byte(out), &raw); err != nil {
		return nil, fmt.Errorf("snapshot json: %v", err)
	}
	s := map[string][]string{}
	for k, v := range raw {
		var l []string
		switch x := v.(type) {
		case []any:
			for _, e := range x {
				l = append(l, fmt.Sprint(e))
			}
		case nil:
		default:
			l = []string{fmt.Sprint(x)}
		}
		sort.Strings(l)
		s[k] = l
	}
	var st Store // which passkeys this PC trusts (ids only: the sign counter changes all the time)
	if readJSON("gate-keys.json", &st) == nil {
		var ids []string
		for _, k := range st.Keys {
			ids = append(ids, k.Name+" "+k.ID+" "+strings.Join(k.Allow, ","))
		}
		sort.Strings(ids)
		s["trusted"] = ids
	}
	return s, nil
}

func diff(old, now []string) string {
	in := map[string]bool{}
	for _, x := range old {
		in[x] = true
	}
	var add, del []string
	for _, x := range now {
		if !in[x] {
			add = append(add, x)
		}
		delete(in, x)
	}
	for x := range in {
		del = append(del, x)
	}
	sort.Strings(del)
	return fmt.Sprintf("added %v removed %v", add, del)
}

func addedTo(old, now []string) []string {
	in := map[string]bool{}
	for _, x := range old {
		in[x] = true
	}
	var add []string
	for _, x := range now {
		if !in[x] {
			add = append(add, x)
		}
	}
	return add
}

func lockScreen() {
	if os.Getenv("WOLGATE_DIR") != "" { // tests must not lock the real PC
		return
	}
	run("schtasks", "/run", "/tn", `WOL\Lock`)
}

func newID() string {
	b := make([]byte, 6)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func watchOnce() {
	snap, err := snapshot()
	if err != nil {
		logf("WATCH: %v", err)
		return
	}
	watchTick(snap)
}

func watchTick(snap map[string][]string) {
	var toKill string
	type fresh struct {
		id, kind string
		added    []string
	}
	var news []fresh
	type neut struct {
		kind      string
		base, cur []string
	}
	var toNeut []neut
	withWatch(func(w *Watch) {
		now := time.Now().Unix()
		w.Alive = now
		if w.Base == nil {
			w.Base = snap
			logf("WATCH: first snapshot saved (%d categories)", len(snap))
			return
		}
		// approved lists from before this service was left out of the snapshot
		var kept []string
		for _, x := range w.Base["services"] {
			if x != "PIEServiceNew" {
				kept = append(kept, x)
			}
		}
		if len(kept) != len(w.Base["services"]) {
			w.Base["services"] = kept
		}
		pending := map[string]bool{}
		shown := map[string]string{} // kind -> what its pending incident told the owner
		for _, in := range w.Inc {
			if in.Status == "pending" {
				pending[in.Kind] = true
				shown[in.Kind] = in.Detail
			}
		}
		for kind, cur := range snap {
			old, known := w.Base[kind]
			if !known {
				w.Base[kind] = cur
				continue
			}
			if kind == "failed" { // every new failed Windows logon is an incident; no approved state
				in := map[string]bool{}
				for _, x := range old {
					in[x] = true
				}
				var failed []string
				for _, x := range cur {
					if !in[x] {
						failed = append(failed, x)
					}
				}
				w.Base[kind] = cur
				if len(failed) == 0 || pending[kind] {
					continue
				}
				w.Inc = append(w.Inc, Incident{ID: newID(), Kind: kind, Detail: fmt.Sprintf("%d failed Windows logon(s)", len(failed)), TS: now, Status: "pending"})
			} else if same := strings.Join(old, "\n") == strings.Join(cur, "\n"); !same && (!pending[kind] || (!dangerous[kind] && shown[kind] != diff(old, cur))) {
				// A lock-only incident can wait for hours, so something new must not hide behind it:
				// the state moved on from what the owner was shown -> a new incident replaces the old one.
				for i := range w.Inc {
					if w.Inc[i].Kind == kind && w.Inc[i].Status == "pending" {
						w.Inc[i].Status = "superseded"
						logf("WATCH SUPERSEDED %s %s (it changed again)", w.Inc[i].ID, kind)
					}
				}
				w.Inc = append(w.Inc, Incident{ID: newID(), Kind: kind, Detail: diff(old, cur), TS: now, Status: "pending", Snap: cur})
			} else {
				// a dangerous kind returning to base does NOT clear its incident (an attacker must not
				// dodge the kill by undoing fast, and our own neutralize must not look like approval):
				// only an app approve/deny/timeout resolves it. Lock-only kinds still auto-clear on undo.
				if same && pending[kind] && !dangerous[kind] {
					for i := range w.Inc {
						if w.Inc[i].Kind == kind && w.Inc[i].Status == "pending" {
							w.Inc[i].Status = "reverted"
							logf("WATCH REVERTED %s %s (back to the approved state)", w.Inc[i].ID, kind)
						}
					}
				}
				continue
			}
			in := w.Inc[len(w.Inc)-1]
			logf("WATCH INCIDENT %s %s: %s", in.ID, kind, in.Detail)
			lockScreen()
			news = append(news, fresh{in.ID, kind, addedTo(old, cur)})
			if neutralizable[kind] {
				toNeut = append(toNeut, neut{kind, append([]string{}, old...), append([]string{}, cur...)})
			}
		}
		for i := range w.Inc {
			in := &w.Inc[i]
			if in.Status == "pending" && dangerous[in.Kind] && now-in.TS > answerWithin {
				in.Status = "timeout"
				logf("WATCH TIMEOUT %s %s", in.ID, in.Kind)
				if dangerous[in.Kind] {
					toKill = fmt.Sprintf("change of %s not approved within %d min", in.Kind, answerWithin/60)
				}
			}
		}
		if len(w.Inc) > 100 {
			w.Inc = w.Inc[len(w.Inc)-100:]
		}
	})
	// look before neutralize: it kills a remote tool, and with it the path we want to report
	for _, f := range news {
		items := explain(f.kind, f.added)
		if items != nil {
			withWatch(func(w *Watch) {
				for i := range w.Inc {
					if w.Inc[i].ID == f.id {
						w.Inc[i].Items = items
					}
				}
			})
			for _, it := range items {
				logf("WATCH WHAT %s: %s | %s | %s %s | %s %v", f.id, it.Name, it.Path, it.Sig, it.Signer, it.Verdict, it.Why)
			}
		}
		tellDesk(deskText(f.kind, items))
	}
	for _, n := range toNeut {
		neutralize(n.kind, n.base, n.cur)
	}
	if toKill != "" {
		kill(toKill)
	}
}

// neutralize puts one dangerous kind back to its approved state right away (before any full kill),
// by running neutralize.ps1 with the approved (base) and current snapshot values for that kind.
func neutralize(kind string, base, cur []string) {
	if os.Getenv("WOLGATE_DIR") != "" { // tests must not touch the real PC
		return
	}
	payload, _ := json.Marshal(map[string]any{"kind": kind, "base": base, "cur": cur})
	c := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedPS(neutralizePS))
	c.Env = append(os.Environ(), "WOLGATE_NEUT="+string(payload))
	out, err := c.CombinedOutput()
	if err != nil {
		logf("WATCH NEUTRALIZE %s FAILED: %v %s", kind, err, strings.TrimSpace(string(out)))
	} else {
		logf("WATCH NEUTRALIZED %s (door shut; approve in the app or it locks down)", kind)
	}
}

func watchLoop() {
	logf("WATCH: started")
	for {
		watchOnce()
		time.Sleep(watchEvery)
	}
}

// ----- gate actions for the server / app -----

// pending incidents for the app: what changed, so the owner can decide (the full log stays in gate.log)
func incidents() string {
	type out struct {
		ID     string       `json:"id"`
		Kind   string       `json:"kind"`
		TS     int64        `json:"ts"`
		Kill   bool         `json:"kill"`
		Detail string       `json:"detail,omitempty"`
		Items  []ChangeItem `json:"items,omitempty"`
	}
	res := struct {
		Alive   int64 `json:"alive"`
		Pending []out `json:"pending"`
	}{Pending: []out{}}
	var w Watch
	readJSON("watch-state.json", &w)
	res.Alive = w.Alive
	for _, in := range w.Inc {
		if in.Status == "pending" {
			res.Pending = append(res.Pending, out{in.ID, in.Kind, in.TS, dangerous[in.Kind], clean(in.Detail, 300), in.Items})
		}
	}
	b, _ := json.Marshal(res)
	return string(b)
}

// approve (signature already checked): the state the owner was shown becomes the approved one. Anything
// newer is not covered and opens its own incident on the next tick. A neutralized kind was already put
// back, so there the current state is what gets approved.
func approve(id string) (string, error) {
	snap, err := snapshot()
	if err != nil {
		return "", err
	}
	msg := "no such pending change"
	withWatch(func(w *Watch) {
		for i := range w.Inc {
			in := &w.Inc[i]
			if in.ID == id && in.Status == "pending" {
				in.Status = "approved"
				if in.Kind != "failed" {
					if in.Snap != nil && !neutralizable[in.Kind] {
						w.Base[in.Kind] = in.Snap
					} else {
						w.Base[in.Kind] = snap[in.Kind]
					}
				}
				msg = "approved: " + in.Kind
				logf("WATCH APPROVED %s %s", in.ID, in.Kind)
			}
		}
	})
	return msg, nil
}

func deny(id string) string {
	found := false
	withWatch(func(w *Watch) {
		for i := range w.Inc {
			if w.Inc[i].ID == id && w.Inc[i].Status == "pending" {
				w.Inc[i].Status = "denied"
				found = true
			}
		}
	})
	if !found {
		return "no such pending change"
	}
	kill("the owner denied a change on this PC")
	return "denied: kill switch started"
}
