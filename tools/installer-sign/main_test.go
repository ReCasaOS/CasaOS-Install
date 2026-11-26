package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// call runs the tool with these arguments and this environment.
func call(t *testing.T, env map[string]string, args ...string) (int, string, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(args, func(name string) string { return env[name] }, &out, &errOut)
	return code, out.String(), errOut.String()
}

func readLine(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(string(data), "\n") || strings.Count(string(data), "\n") != 1 {
		t.Fatalf("%s is not one line ending with a newline: %q", path, data)
	}
	return strings.TrimSuffix(string(data), "\n")
}

// keys makes a key pair with keygen and returns its directory, the private and the public line.
func keys(t *testing.T) (string, string, string) {
	t.Helper()
	dir := t.TempDir()
	code, out, errOut := call(t, nil, "keygen", "--out", dir)
	if code != 0 {
		t.Fatalf("keygen: %d %s", code, errOut)
	}
	private := readLine(t, filepath.Join(dir, "installer-signing.key"))
	public := readLine(t, filepath.Join(dir, "installer-signing.pub"))
	if strings.Contains(out+errOut, private) {
		t.Fatal("keygen printed the private key")
	}
	if !strings.Contains(out, public) {
		t.Fatalf("keygen did not print the public key: %s", out)
	}
	keyPath := filepath.Join(dir, "installer-signing.key")
	for _, command := range []string{
		`Get-Content "` + keyPath + `" | gh secret set INSTALLER_SIGNING_KEY --env release-signing`,
		`gh secret set INSTALLER_SIGNING_KEY --env release-signing -R ReCasaOS/CasaOS-Install < "` + keyPath + `"`,
	} {
		if !strings.Contains(out, command) {
			t.Fatalf("keygen did not print %q: %s", command, out)
		}
	}
	return dir, private, public
}

func TestKeygen(t *testing.T) {
	dir, private, public := keys(t)
	priv, err := base64.StdEncoding.DecodeString(private)
	if err != nil || len(priv) != ed25519.PrivateKeySize {
		t.Fatalf("the private key is not base64 of %d bytes: %v", ed25519.PrivateKeySize, err)
	}
	pub, err := base64.StdEncoding.DecodeString(public)
	if err != nil || !bytes.Equal(pub, ed25519.PrivateKey(priv).Public().(ed25519.PublicKey)) {
		t.Fatalf("the public key is not the private key's: %v", err)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(filepath.Join(dir, "installer-signing.key"))
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("the private key file is not 0600: %v %v", info.Mode(), err)
		}
	}
	// a second keygen in the same directory changes nothing
	code, _, errOut := call(t, nil, "keygen", "--out", dir)
	if code == 0 || errOut == "" {
		t.Fatal("keygen overwrote existing keys")
	}
	if readLine(t, filepath.Join(dir, "installer-signing.key")) != private {
		t.Fatal("the private key changed")
	}
}

func TestSignVerify(t *testing.T) {
	dir, private, public := keys(t)
	work := t.TempDir()
	script := filepath.Join(work, "install.sh")
	content := []byte("#!/usr/bin/bash\nreadonly CASAOS_INSTALL_RELEASE_TAG=\"v1.2.3\"\n")
	if err := os.WriteFile(script, content, 0o644); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{"KEY": private + "\n"}
	code, out, errOut := call(t, env, "sign", "--key-env", "KEY", script)
	if code != 0 {
		t.Fatalf("sign: %d %s", code, errOut)
	}
	if strings.Contains(out+errOut, private) {
		t.Fatal("sign printed the private key")
	}
	// the contract with the core: one line of base64 of the raw signature of the exact bytes
	sig, err := base64.StdEncoding.DecodeString(readLine(t, script+".sig"))
	if err != nil || len(sig) != ed25519.SignatureSize {
		t.Fatalf("install.sh.sig is not base64 of %d bytes: %v", ed25519.SignatureSize, err)
	}
	pub, _ := base64.StdEncoding.DecodeString(public)
	if !ed25519.Verify(pub, content, sig) {
		t.Fatal("the signature is not one of the file's bytes")
	}

	pubFile := filepath.Join(dir, "installer-signing.pub")
	if code, _, errOut := call(t, nil, "verify", "--pub", pubFile, script); code != 0 {
		t.Fatalf("verify: %d %s", code, errOut)
	}
	// a public key file may begin with comment lines
	commented := filepath.Join(work, "commented.pub")
	if err := os.WriteFile(commented, []byte("# replace with key A\r\n"+public+"\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, _, errOut := call(t, nil, "verify", "--pub", commented, script); code != 0 {
		t.Fatalf("verify with a commented key: %d %s", code, errOut)
	}
	// another key
	otherDir, _, _ := keys(t)
	if code, _, _ := call(t, nil, "verify", "--pub", filepath.Join(otherDir, "installer-signing.pub"), script); code != 1 {
		t.Fatalf("verify with another key: %d, want 1", code)
	}
	// one byte changed
	if err := os.WriteFile(script, append(content, ' '), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, _, errOut := call(t, nil, "verify", "--pub", pubFile, script); code != 1 || errOut == "" {
		t.Fatalf("verify of a changed file: %d %q, want 1 and a sentence", code, errOut)
	}
	// no signature at all
	os.Remove(script + ".sig")
	if code, _, _ := call(t, nil, "verify", "--pub", pubFile, script); code != 1 {
		t.Fatalf("verify with no signature: %d, want 1", code)
	}
}

func TestSignRefusesABadKey(t *testing.T) {
	_, private, _ := keys(t)
	script := filepath.Join(t.TempDir(), "install.sh")
	if err := os.WriteFile(script, []byte("echo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	short := base64.StdEncoding.EncodeToString(make([]byte, 32))
	for name, value := range map[string]string{
		"empty":      "",
		"not base64": "not base64!",
		"too short":  short,
		"two lines":  private + "\n" + private,
	} {
		code, out, errOut := call(t, map[string]string{"KEY": value}, "sign", "--key-env", "KEY", script)
		if code == 0 {
			t.Errorf("%s: sign accepted the key", name)
		}
		if value != "" && strings.Contains(out+errOut, value) {
			t.Errorf("%s: sign printed the value of the variable", name)
		}
		if _, err := os.Stat(script + ".sig"); err == nil {
			t.Errorf("%s: a signature was written", name)
			os.Remove(script + ".sig")
		}
	}
	if code, _, _ := call(t, nil, "sign", script); code == 0 {
		t.Error("sign ran without --key-env")
	}
}
