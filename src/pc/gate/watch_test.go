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
	if strings.Contains(incidents(), "evil") {
		t.Fatal("details must stay on the PC")
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
