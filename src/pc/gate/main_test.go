package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"os"
	"testing"
	"time"
)

const site = "https://auth.example.com"

var b64 = base64.RawURLEncoding.EncodeToString

func setup(t *testing.T) *ecdsa.PrivateKey {
	dir = t.TempDir()
	os.WriteFile(path("agent.json"), []byte("\xef\xbb\xbf"+`{"name":"PC1"}`), 0o600)
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	der, _ := x509.MarshalPKIXPublicKey(&priv.PublicKey)
	writeJSON("gate-keys.json", &Store{Origin: site, Keys: []Key{{ID: "k1", Pub: b64(der), Alg: -7, Name: "Phone", Allow: []string{"all"}}}})
	return priv
}

var counter uint32

func sign(priv *ecdsa.PrivateKey, machine string, r *Req, origin string) {
	counter++
	cd, _ := json.Marshal(map[string]string{"type": "webauthn.get", "challenge": challenge(machine, r), "origin": origin})
	rp := sha256.Sum256([]byte("auth.example.com"))
	ad := append(rp[:], 0x05, 0, 0, 0, 0)
	binary.BigEndian.PutUint32(ad[33:], counter)
	cdh := sha256.Sum256(cd)
	d := sha256.Sum256(append(append([]byte{}, ad...), cdh[:]...))
	sig, _ := ecdsa.SignASN1(rand.Reader, priv, d[:])
	r.Cred = &Cred{ID: "k1", ClientDataJSON: b64(cd), AuthenticatorData: b64(ad), Signature: b64(sig)}
}

func check(t *testing.T, r *Req) error {
	var st Store
	readJSON("gate-keys.json", &st)
	s := State{Nonces: map[string]int64{}}
	readJSON("gate-state.json", &s)
	if s.Nonces == nil {
		s.Nonces = map[string]int64{}
	}
	_, err := verify(&st, &s, r)
	if err == nil {
		writeJSON("gate-keys.json", &st)
		writeJSON("gate-state.json", &s)
	}
	return err
}

func req(act string) *Req {
	return &Req{Act: act, TS: time.Now().Unix(), Nonce: "nonce-" + time.Now().Format("150405.000000000")}
}

func TestGate(t *testing.T) {
	priv := setup(t)

	r := req("who")
	sign(priv, "PC1", r, site)
	if err := check(t, r); err != nil {
		t.Fatalf("good signature refused: %v", err)
	}
	if err := check(t, r); err == nil {
		t.Fatal("replayed request accepted")
	}

	r = req("who")
	sign(priv, "PC1", r, site)
	r.Act = "shutdown" // server swaps the action after the user signed "who"
	if err := check(t, r); err == nil {
		t.Fatal("swapped action accepted")
	}

	r = req("who")
	sign(priv, "PC2", r, site) // signature for another PC
	if err := check(t, r); err == nil {
		t.Fatal("signature for another PC accepted")
	}

	r = req("who")
	sign(priv, "PC1", r, "https://evil.example.com")
	if err := check(t, r); err == nil {
		t.Fatal("other origin accepted")
	}

	r = req("who")
	r.TS -= 600
	sign(priv, "PC1", r, site)
	if err := check(t, r); err == nil {
		t.Fatal("old request accepted")
	}

	other, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	r = req("who")
	sign(other, "PC1", r, site) // attacker's own key under a trusted id
	if err := check(t, r); err == nil {
		t.Fatal("foreign key accepted")
	}

	var st Store
	readJSON("gate-keys.json", &st)
	st.Keys[0].Allow = []string{"lock"}
	writeJSON("gate-keys.json", &st)
	r = req("shutdown")
	sign(priv, "PC1", r, site)
	if err := check(t, r); err == nil {
		t.Fatal("action outside the key's allow list accepted")
	}

	r = req("lock")
	sign(priv, "PC1", r, site)
	if err := check(t, r); err != nil {
		t.Fatalf("allowed action refused: %v", err)
	}
}
