package main

// Tells the owner WHAT changed, so a Steam install and an attack don't look the same: for a new service,
// task or remote tool it finds the file, its signature and folder and says "normal" or "check".
// "normal" is a hint, never proof: a valid signature doesn't mean the owner wanted it (AnyDesk is signed
// too), so the wording always ends with "if it wasn't you, deny".

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unicode"
)

//go:embed explain.ps1
var explainPS string

const maxExplain = 5 // items looked up per incident

var explainable = map[string]bool{"services": true, "tasks": true, "remote": true}

// A signed program that runs whatever it is told: its Microsoft signature says nothing about the job.
var runners = map[string]bool{"powershell": true, "pwsh": true, "cmd": true, "wscript": true, "cscript": true, "mshta": true,
	"rundll32": true, "regsvr32": true, "svchost": true, "msiexec": true, "python": true, "pythonw": true, "node": true,
	"java": true, "javaw": true, "wmic": true, "bitsadmin": true, "certutil": true, "bash": true, "wsl": true, "conhost": true,
	"schtasks": true, "sc": true, "net": true, "curl": true}

type ChangeItem struct {
	Name    string   `json:"name"`
	Path    string   `json:"path,omitempty"`
	Args    string   `json:"args,omitempty"`
	Account string   `json:"account,omitempty"`
	Sig     string   `json:"sig,omitempty"`
	Signer  string   `json:"signer,omitempty"`
	Verdict string   `json:"verdict"`       // normal | check
	Why     []string `json:"why,omitempty"` // no_file unsigned bad_signature odd_folder runner
}

// text from the outside world: no control characters, no surprise length
func clean(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, s)
	s = strings.TrimSpace(s)
	if r := []rune(s); len(r) > max {
		return string(r[:max-1]) + "…"
	}
	return s
}

func usualFolder(p string) bool {
	p = strings.ToLower(filepath.Clean(p)) + `\`
	win := strings.ToLower(os.Getenv("windir"))
	if win == "" {
		win = `c:\windows`
	}
	if strings.HasPrefix(p, win+`\temp\`) {
		return false
	}
	for _, root := range []string{win, os.Getenv("ProgramFiles"), os.Getenv("ProgramFiles(x86)"), `c:\program files`, `c:\program files (x86)`} {
		if root != "" && strings.HasPrefix(p, strings.ToLower(root)+`\`) {
			return true
		}
	}
	return false
}

func judge(it *ChangeItem) {
	it.Why = nil
	if it.Path == "" || it.Sig == "NoFile" {
		it.Why = append(it.Why, "no_file")
	} else {
		switch it.Sig {
		case "Valid":
		case "HashMismatch", "NotTrusted", "Incompatible":
			it.Why = append(it.Why, "bad_signature")
		default:
			it.Why = append(it.Why, "unsigned")
		}
		if !usualFolder(it.Path) {
			it.Why = append(it.Why, "odd_folder")
		}
		base := strings.ToLower(filepath.Base(it.Path))
		if runners[strings.TrimSuffix(base, filepath.Ext(base))] {
			it.Why = append(it.Why, "runner")
		}
	}
	it.Verdict = "normal"
	if len(it.Why) > 0 {
		it.Verdict = "check"
	}
}

func explainRun(kind string, names []string) ([]ChangeItem, error) {
	if len(names) > maxExplain {
		names = names[:maxExplain]
	}
	payload, _ := json.Marshal(map[string]any{"kind": kind, "names": names})
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	c := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedPS(explainPS))
	c.Env = append(os.Environ(), "WOLGATE_EXPLAIN="+string(payload))
	out, err := c.Output()
	if err != nil {
		return nil, fmt.Errorf("explain: %v", err)
	}
	var items []ChangeItem
	if err := json.Unmarshal(out, &items); err != nil {
		return nil, fmt.Errorf("explain json: %v", err)
	}
	for i := range items {
		it := &items[i]
		it.Name, it.Path, it.Args = clean(it.Name, 120), clean(it.Path, 200), clean(it.Args, 120)
		it.Account, it.Signer, it.Sig = clean(it.Account, 60), clean(it.Signer, 80), clean(it.Sig, 30)
		judge(it)
	}
	return items, nil
}

func explain(kind string, names []string) []ChangeItem {
	if !explainable[kind] || len(names) == 0 || os.Getenv("WOLGATE_DIR") != "" { // tests stay off the real PC
		return nil
	}
	items, err := explainRun(kind, names)
	if err != nil {
		logf("WATCH: %v", err)
		return nil
	}
	return items
}

// What the person at the desk reads. Comes from this PC only, so it stays true even if the server is not.
// msg.exe shows about 255 characters: the verdict and the name go first.
func deskText(kind string, items []ChangeItem) string {
	if len(items) == 0 {
		if neutralizable[kind] {
			return fmt.Sprintf("change detected (%s) - door shut + screen locked. Approve it in the app, or the PC locks down fully.", kind)
		}
		return fmt.Sprintf("change detected (%s) - screen locked. Approve it with your fingerprint in the app.", kind)
	}
	it := items[0] // the worst one speaks for all
	for _, x := range items {
		if x.Verdict != "normal" {
			it = x
			break
		}
	}
	label :=map[string]string{"services": "service", "tasks": "scheduled task", "remote": "remote-control tool"}[kind]
	more := ""
	if len(items) > 1 {
		more = fmt.Sprintf(" (+%d more)", len(items)-1)
	}
	signed := "NOT signed"
	if it.Sig == "Valid" && it.Signer != "" {
		signed = "signed: " + strings.TrimRight(clean(it.Signer, 40), ".")
	}
	head := fmt.Sprintf("Looks like a normal install: new %s \"%s\"%s, %s.", label, clean(it.Name, 40), more, signed)
	if it.Verdict != "normal" {
		head = fmt.Sprintf("SUSPICIOUS (%s): new %s \"%s\"%s, %s.", strings.Join(it.Why, ", "), label, clean(it.Name, 40), more, signed)
	}
	if kind == "remote" {
		head = fmt.Sprintf("REMOTE TOOL started: \"%s\"%s, %s.", clean(it.Name, 40), more, signed)
	}
	return clean(fmt.Sprintf("%s Screen locked. Was it you? Approve in the app. If not: Deny. File: %s", head, it.Path), 240)
}
