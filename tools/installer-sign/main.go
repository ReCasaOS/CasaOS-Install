// installer-sign makes the Ed25519 keys of the installer, signs install.sh and
// checks a signature, with the standard library only.
//
//	installer-sign keygen --out DIR
//	installer-sign sign --key-env NAME FILE
//	installer-sign verify --pub PUBFILE FILE
//
// A key is one line of standard base64: the 64-byte private key, or the 32-byte
// public key. FILE.sig is one line of standard base64 of the raw 64-byte
// signature of the exact bytes of FILE. A public key file may begin with lines
// that start with #.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

func main() {
	os.Exit(run(os.Args[1:], os.Getenv, os.Stdout, os.Stderr))
}

const usage = `usage:
  installer-sign keygen --out DIR
  installer-sign sign --key-env NAME FILE
  installer-sign verify --pub PUBFILE FILE
`

// run returns the exit status: 0 done, 1 a signature that does not verify, 2 any other error.
func run(args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	flags := flag.NewFlagSet(args[0], flag.ContinueOnError)
	flags.SetOutput(stderr)
	var err error
	switch args[0] {
	case "keygen":
		out := flags.String("out", "", "directory to write installer-signing.key and installer-signing.pub into")
		if flags.Parse(args[1:]) != nil || *out == "" || flags.NArg() != 0 {
			fmt.Fprint(stderr, usage)
			return 2
		}
		err = keygen(*out, stdout)
	case "sign":
		keyEnv := flags.String("key-env", "", "environment variable that holds the private key")
		if flags.Parse(args[1:]) != nil || *keyEnv == "" || flags.NArg() != 1 {
			fmt.Fprint(stderr, usage)
			return 2
		}
		err = sign(getenv(*keyEnv), *keyEnv, flags.Arg(0), stdout)
	case "verify":
		pub := flags.String("pub", "", "public key file")
		if flags.Parse(args[1:]) != nil || *pub == "" || flags.NArg() != 1 {
			fmt.Fprint(stderr, usage)
			return 2
		}
		var bad bool
		bad, err = verify(*pub, flags.Arg(0), stdout)
		if bad {
			fmt.Fprintln(stderr, err)
			return 1
		}
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	return 0
}

func keygen(dir string, stdout io.Writer) error {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	keyPath := filepath.Join(dir, "installer-signing.key")
	pubPath := filepath.Join(dir, "installer-signing.pub")
	for _, path := range []string{keyPath, pubPath} {
		if _, err := os.Lstat(path); err == nil {
			return fmt.Errorf("%s already exists: nothing was written", path)
		}
	}
	if err := writeNew(keyPath, base64.StdEncoding.EncodeToString(private), 0o600); err != nil {
		return err
	}
	pubLine := base64.StdEncoding.EncodeToString(public)
	if err := writeNew(pubPath, pubLine, 0o644); err != nil {
		return err
	}
	fmt.Fprintf(stdout, `Public key:
%s

It is written in %s.
The private key is in %s.
Never commit it and never paste it anywhere. Give it to the release-signing
environment of the repository with, in PowerShell:

  Get-Content "%s" | gh secret set INSTALLER_SIGNING_KEY --env release-signing -R ReCasaOS/CasaOS-Install

or in a POSIX shell:

  gh secret set INSTALLER_SIGNING_KEY --env release-signing -R ReCasaOS/CasaOS-Install < "%s"

then keep the file offline (a password manager) and delete it from this machine.
`, pubLine, pubPath, keyPath, keyPath, keyPath)
	return nil
}

// writeNew writes one line into a file that must not exist yet.
func writeNew(path, line string, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(line + "\n"); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func sign(value, name, file string, stdout io.Writer) error {
	// the value of the variable never appears in an error
	if strings.TrimSpace(value) == "" {
		return fmt.Errorf("the environment variable %s is empty: nothing was signed", name)
	}
	key, err := decodeLine(value, ed25519.PrivateKeySize)
	if err != nil {
		return fmt.Errorf("the environment variable %s does not hold one line of base64 of a %d-byte Ed25519 private key: nothing was signed", name, ed25519.PrivateKeySize)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	sig := ed25519.Sign(ed25519.PrivateKey(key), data)
	if err := os.WriteFile(file+".sig", []byte(base64.StdEncoding.EncodeToString(sig)+"\n"), 0o644); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "%s.sig written\n", file)
	return nil
}

// verify says whether file.sig is a signature of file by the key of pubFile. bad
// is true when the files are readable and the signature is missing or wrong.
func verify(pubFile, file string, stdout io.Writer) (bad bool, err error) {
	raw, err := os.ReadFile(pubFile)
	if err != nil {
		return false, err
	}
	var lines []string
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "#") {
			lines = append(lines, line)
		}
	}
	if len(lines) != 1 {
		return false, fmt.Errorf("%s does not hold one public key line", pubFile)
	}
	pub, err := decodeLine(lines[0], ed25519.PublicKeySize)
	if err != nil {
		return false, fmt.Errorf("%s does not hold base64 of a %d-byte Ed25519 public key", pubFile, ed25519.PublicKeySize)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return false, err
	}
	sigText, err := os.ReadFile(file + ".sig")
	if errors.Is(err, os.ErrNotExist) {
		return true, fmt.Errorf("%s has no signature: %s.sig is missing", file, file)
	}
	if err != nil {
		return false, err
	}
	sig, err := decodeLine(string(sigText), ed25519.SignatureSize)
	if err != nil || !ed25519.Verify(ed25519.PublicKey(pub), data, sig) {
		return true, fmt.Errorf("%s.sig is not a signature of %s by the key in %s", file, file, pubFile)
	}
	fmt.Fprintf(stdout, "%s.sig is a valid signature of %s by the key in %s\n", file, file, pubFile)
	return false, nil
}

// decodeLine decodes one line of standard base64 that must give size bytes.
func decodeLine(text string, size int) ([]byte, error) {
	text = strings.TrimRight(text, "\r\n")
	if strings.ContainsAny(text, "\r\n") {
		return nil, errors.New("more than one line")
	}
	data, err := base64.StdEncoding.DecodeString(strings.TrimSpace(text))
	if err != nil || len(data) != size {
		return nil, errors.New("wrong length")
	}
	return data, nil
}
