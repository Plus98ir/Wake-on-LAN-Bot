package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func call(h http.Handler, path, body string, hdr bool) map[string]string {
	r := httptest.NewRequest("POST", "http://"+rescueAddr+path, strings.NewReader(body))
	r.Host = rescueAddr
	if hdr {
		r.Header.Set("X-Wolbot", "1")
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	out := map[string]string{"code": http.StatusText(w.Code)}
	json.Unmarshal(w.Body.Bytes(), &out)
	return out
}

func TestRescuePassword(t *testing.T) {
	dir = t.TempDir()
	os.Setenv("WOLGATE_DIR", dir)
	setup := rescueMux(true)
	if call(setup, "/api/setpass", `{"password":"short"}`, true)["state"] != "setup" {
		t.Fatal("short password accepted")
	}
	if call(setup, "/api/setpass", `{"password":"correct horse 9"}`, false)["code"] != "Forbidden" {
		t.Fatal("request without the app header accepted")
	}
	if call(setup, "/api/setpass", `{"password":"correct horse 9"}`, true)["state"] != "saved" {
		t.Fatal("password not saved")
	}
	h := rescueMux(false)
	for i := 0; i < 5; i++ {
		if r := call(h, "/api/pass", `{"password":"wrong"}`, true); !strings.Contains(r["msg"], "wrong") {
			t.Fatalf("try %d: %v", i, r)
		}
	}
	if r := call(h, "/api/pass", `{"password":"correct horse 9"}`, true); !strings.Contains(r["msg"], "wait") {
		t.Fatalf("no lockout after 5 wrong passwords: %v", r)
	}
	var rec Recovery
	readJSON("recovery.json", &rec)
	rec.Until = 0
	writeJSON("recovery.json", &rec)
	if r := call(h, "/api/pass", `{"password":"correct horse 9"}`, true); r["state"] != "network" {
		t.Fatalf("right password refused: %v", r)
	}
	if strings.Contains(string(mustRead(t, "recovery.json")), "correct horse") {
		t.Fatal("password stored in clear")
	}
}

func mustRead(t *testing.T, n string) []byte {
	b, err := os.ReadFile(path(n))
	if err != nil {
		t.Fatal(err)
	}
	return b
}
