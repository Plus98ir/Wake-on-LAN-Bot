package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestDiff(t *testing.T) {
	d := diff([]string{"a", "b"}, []string{"b", "c"})
	if !strings.Contains(d, "added [c]") || !strings.Contains(d, "removed [a]") {
		t.Fatal(d)
	}
}

func TestIncidentsAndDeny(t *testing.T) {
	dir = t.TempDir()
	os.Setenv("WOLGATE_DIR", dir)
	writeJSON("watch-state.json", &Watch{Base: map[string][]string{"admins": {"me"}}, Alive: 42, Inc: []Incident{
		{ID: "aa11", Kind: "admins", Detail: "added [evil]", TS: 1, Status: "pending"},
		{ID: "bb22", Kind: "tasks", Detail: "x", TS: 1, Status: "approved"},
	}})
	var res struct {
		Alive   int64
		Pending []struct {
			ID   string
			Kind string
			Kill bool
		}
	}
	json.Unmarshal([]byte(incidents()), &res)
	if res.Alive != 42 || len(res.Pending) != 1 || res.Pending[0].ID != "aa11" || !res.Pending[0].Kill {
		t.Fatalf("incidents: %+v", res)
	}
	if !strings.Contains(incidents(), "evil") {
		t.Fatal("the owner must see what changed")
	}
	if deny("nope") != "no such pending change" {
		t.Fatal("unknown id accepted")
	}
	if !strings.HasPrefix(deny("aa11"), "denied") {
		t.Fatal("deny failed")
	}
	var w Watch
	readJSON("watch-state.json", &w)
	if w.Inc[0].Status != "denied" {
		t.Fatal("status not saved")
	}
}

func tickState(t *testing.T) func() Watch {
	dir = t.TempDir()
	os.Setenv("WOLGATE_DIR", dir)
	writeJSON("watch-state.json", &Watch{Base: map[string][]string{"services": {"a"}}})
	return func() Watch {
		var w Watch
		readJSON("watch-state.json", &w)
		return w
	}
}

// the Steam case: one new service = one incident that just waits, however long the owner takes
func TestLockOnlyWaitsWithoutRepeating(t *testing.T) {
	state := tickState(t)
	s := map[string][]string{"services": {"a", "steam"}}
	watchTick(s)
	withWatch(func(w *Watch) { w.Inc[0].TS -= 3600 })
	watchTick(s)
	watchTick(s)
	w := state()
	if len(w.Inc) != 1 || w.Inc[0].Status != "pending" {
		t.Fatalf("incidents: %+v", w.Inc)
	}
}

// something new must not slip through on the approval of what was shown earlier
func TestSecondChangeCannotHideBehindPending(t *testing.T) {
	state := tickState(t)
	watchTick(map[string][]string{"services": {"a", "steam"}})
	watchTick(map[string][]string{"services": {"a", "evil", "steam"}})
	w := state()
	if len(w.Inc) != 2 || w.Inc[0].Status != "superseded" || w.Inc[1].Status != "pending" || !strings.Contains(w.Inc[1].Detail, "evil") {
		t.Fatalf("incidents: %+v", w.Inc)
	}
	if msg, _ := approve(w.Inc[0].ID); msg != "no such pending change" {
		t.Fatalf("approved what was replaced: %s", msg)
	}
}

// approving a pending change accepts what was shown, not what is there by now
func TestApproveCoversOnlyWhatWasShown(t *testing.T) {
	state := tickState(t)
	watchTick(map[string][]string{"services": {"a", "steam"}})
	id := state().Inc[0].ID
	if msg, err := approve(id); err != nil || !strings.HasPrefix(msg, "approved") {
		t.Fatalf("approve: %v %v", msg, err)
	}
	if b := strings.Join(state().Base["services"], ","); b != "a,steam" {
		t.Fatalf("approved more than was shown: %s", b)
	}
	watchTick(map[string][]string{"services": {"a", "evil", "steam"}})
	w := state()
	if last := w.Inc[len(w.Inc)-1]; last.Status != "pending" || !strings.Contains(last.Detail, "added [evil]") {
		t.Fatalf("evil got through: %+v", w.Inc)
	}
}

// a dangerous kind keeps its deadline: nothing softened
func TestDangerousStillTimesOut(t *testing.T) {
	state := tickState(t)
	withWatch(func(w *Watch) { w.Base["keys"] = []string{"k1"} })
	s := map[string][]string{"keys": {"k2"}}
	watchTick(s)
	withWatch(func(w *Watch) { w.Inc[0].TS -= answerWithin + 1 })
	watchTick(s)
	if w := state(); w.Inc[0].Status != "timeout" {
		t.Fatalf("incidents: %+v", w.Inc)
	}
	if b, _ := os.ReadFile(path("gate.log")); !strings.Contains(string(b), "KILL SWITCH") {
		t.Fatal("no kill")
	}
}

func TestJudge(t *testing.T) {
	cases := []struct {
		it   ChangeItem
		want string
		why  string
	}{
		{ChangeItem{Path: `C:\Program Files (x86)\Common Files\Steam\steamservice.exe`, Sig: "Valid"}, "normal", ""},
		{ChangeItem{Path: `C:\Users\Public\x.exe`, Sig: "NotSigned"}, "check", "unsigned odd_folder"},
		{ChangeItem{Path: `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, Sig: "Valid"}, "check", "runner"},
		{ChangeItem{Path: `C:\Windows\Temp\a.exe`, Sig: "Valid"}, "check", "odd_folder"},
		{ChangeItem{Path: `C:\Program Files\..\Users\me\a.exe`, Sig: "Valid"}, "check", "odd_folder"},
		{ChangeItem{Path: `C:\Program Files\a.exe`, Sig: "HashMismatch"}, "check", "bad_signature"},
		{ChangeItem{Sig: "NoFile"}, "check", "no_file"},
	}
	for _, c := range cases {
		judge(&c.it)
		if c.it.Verdict != c.want || strings.Join(c.it.Why, " ") != c.why {
			t.Errorf("%+v want %s %q", c.it, c.want, c.why)
		}
	}
}

func TestDeskTextFits(t *testing.T) {
	long := strings.Repeat("x", 300)
	txt := "wolbot: " + deskText("services", []ChangeItem{{Name: long, Path: long, Signer: long, Sig: "Valid", Verdict: "check", Why: []string{"odd_folder", "runner"}}})
	if n := len([]rune(txt)); n > 255 {
		t.Fatalf("too long for msg.exe: %d", n)
	}
}
