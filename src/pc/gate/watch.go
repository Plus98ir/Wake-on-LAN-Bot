package main

// Watchdog (`wol-gate watch`, task WOL\Watch as SYSTEM): every 15 s it compares a snapshot of what
// decides who can get into this PC with the approved one. A change locks the screen and waits for
// the owner: approve = a passkey signature over that incident, checked here (act "approve");
// deny (act "deny") or no answer to a dangerous change within 3 min = kill switch.

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

const (
	watchEvery   = 15 * time.Second
	answerWithin = 180 // seconds to approve a dangerous change before the kill switch
)

// changes that open a way into the PC: no answer = kill switch. The rest only stay locked + logged.
var dangerous = map[string]bool{"admins": true, "users": true, "firewall": true, "uac": true, "rdp": true,
	"keys": true, "trusted": true, "remote": true, "failed": true}

type Incident struct {
	ID     string `json:"id"`
	Kind   string `json:"kind"`
	Detail string `json:"detail"`
	TS     int64  `json:"ts"`
	Status string `json:"status"` // pending | approved | denied | timeout
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
	var toKill string
	withWatch(func(w *Watch) {
		now := time.Now().Unix()
		w.Alive = now
		if w.Base == nil {
			w.Base = snap
			logf("WATCH: first snapshot saved (%d categories)", len(snap))
			return
		}
		pending := map[string]bool{}
		for _, in := range w.Inc {
			if in.Status == "pending" {
				pending[in.Kind] = true
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
				var fresh []string
				for _, x := range cur {
					if !in[x] {
						fresh = append(fresh, x)
					}
				}
				w.Base[kind] = cur
				if len(fresh) == 0 || pending[kind] {
					continue
				}
				w.Inc = append(w.Inc, Incident{ID: newID(), Kind: kind, Detail: fmt.Sprintf("%d failed Windows logon(s)", len(fresh)), TS: now, Status: "pending"})
			} else if strings.Join(old, "\n") != strings.Join(cur, "\n") && !pending[kind] {
				w.Inc = append(w.Inc, Incident{ID: newID(), Kind: kind, Detail: diff(old, cur), TS: now, Status: "pending"})
			} else {
				continue
			}
			in := w.Inc[len(w.Inc)-1]
			logf("WATCH INCIDENT %s %s: %s", in.ID, kind, in.Detail)
			run("schtasks", "/run", "/tn", `WOL\Lock`)
			tellDesk(fmt.Sprintf("change detected (%s) - screen locked. Approve it with your fingerprint in the app.", kind))
		}
		for i := range w.Inc {
			in := &w.Inc[i]
			if in.Status == "pending" && now-in.TS > answerWithin {
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
	if toKill != "" {
		kill(toKill)
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

// pending incidents without details (details stay in gate.log on this PC)
func incidents() string {
	type out struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
		TS   int64  `json:"ts"`
		Kill bool   `json:"kill"`
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
			res.Pending = append(res.Pending, out{in.ID, in.Kind, in.TS, dangerous[in.Kind]})
		}
	}
	b, _ := json.Marshal(res)
	return string(b)
}

// approve (signature already checked): the current state of that kind becomes the approved one
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
					w.Base[in.Kind] = snap[in.Kind]
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
