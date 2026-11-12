//go:build windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"unsafe"

	"golang.org/x/sys/windows"
)

func TestChildAccessPlan(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "agent")
	models := filepath.Join(root, "models")
	for _, d := range []string{data, models, filepath.Join(models, "whisper"), filepath.Join(data, "keys")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(data, "config.yaml"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	sp := ChildSpec{Name: "stt", Cmd: "never-on-path-xyz", Cwd: filepath.Join(data, "stt"),
		Env: []string{"OLLAMA_MODELS=" + models, "PATH=" + models + ";" + root, "OLLAMA_HOST=127.0.0.1:11434"},
		Args: []string{"app.py", "--log-file", filepath.Join(data, "stt", "stt.log"), "--model=" + filepath.Join(models, "whisper"),
			"relative\\path", "--config", filepath.Join(data, "config.yaml")},
		ReadPaths: []string{filepath.Join(root, "extra")}, WritePaths: []string{filepath.Join(data, "STT")}}
	plan := childAccessPlan(sp, data)
	got := map[string]ChildAccess{}
	for i, a := range plan {
		got[strings.ToLower(a.Path)] = a
		if i > 0 && len(plan[i-1].Path) > len(a.Path) {
			t.Fatalf("parents must come first: %+v", plan)
		}
	}
	want := map[string]string{ // path -> need/noGrant
		filepath.Join(data, "stt"):         needModify,
		models:                             needRead,
		filepath.Join(models, "whisper"):   needRead,
		filepath.Join(root, "extra"):       needRead,
		filepath.Join(data, "config.yaml"): "own",
	}
	if len(plan) != len(want) {
		t.Fatalf("want %d entries, got %+v", len(want), plan)
	}
	for p, need := range want {
		a, ok := got[strings.ToLower(p)]
		if !ok {
			t.Fatalf("missing %s in %+v", p, plan)
		}
		if need == "own" {
			if !strings.Contains(a.noGrant, "agent's own folder") {
				t.Fatalf("the agent's own files must never be granted: %+v", a)
			}
			continue
		}
		if a.Need != need || a.noGrant != "" {
			t.Fatalf("%s: want %s, got %+v", p, need, a)
		}
	}
	if a := got[strings.ToLower(filepath.Join(data, "stt"))]; !a.ownData {
		t.Fatalf("the child's folder under the data folder is its own: %+v", a)
	}
}

func TestTooBroad(t *testing.T) {
	t.Setenv("SystemDrive", "C:")
	t.Setenv("SystemRoot", `C:\Windows`)
	t.Setenv("ProgramFiles", `C:\Program Files`)
	t.Setenv("ProgramData", `C:\ProgramData`)
	users := `C:\Users`
	profile := filepath.Join(users, "someone") // built here: the public-data scan flags literal profile paths
	for p, want := range map[string]string{
		`C:\`:              "drive root",
		`C:\Windows`:       "system folder",
		`c:\program files`: "system folder",
		`C:\ProgramData`:   "system folder",
		users:              "system folder",
		profile:            "whole user profile",
		filepath.Join(profile, ".ollama", "models"): "",
		`C:\Program Files\Ollama`:                   "",
		`D:\models`:                                 "",
	} {
		got := tooBroad(p)
		if (want == "") != (got == "") || !strings.Contains(got, want) {
			t.Errorf("%s: want %q, got %q", p, want, got)
		}
	}
}

func TestEnsureAccess(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "agent")
	dirs := map[string]string{}
	for _, n := range []string{"ok", "grantme", "failgrant", "stillno", "broad"} {
		dirs[n] = filepath.Join(root, n)
		if err := os.MkdirAll(dirs[n], 0o755); err != nil {
			t.Fatal(err)
		}
	}
	own := filepath.Join(data, "stt")
	mk := func() []ChildAccess {
		return []ChildAccess{
			{Path: dirs["ok"], Need: needRead},
			{Path: dirs["grantme"], Need: needRead},
			{Path: dirs["failgrant"], Need: needRead},
			{Path: dirs["stillno"], Need: needRead},
			{Path: dirs["broad"], Need: needRead, noGrant: "a system folder"},
			{Path: filepath.Join(root, "absent"), Need: needRead},
			{Path: own, Need: needModify, ownData: true},
		}
	}
	granted := map[string]bool{}
	check := func(a ChildAccess) error {
		switch {
		case a.Path == dirs["ok"], granted[a.Path]:
			return nil
		}
		return errors.New("Access is denied.")
	}
	grant := func(a ChildAccess) error {
		switch a.Path {
		case dirs["failgrant"]:
			return errors.New("icacls failed")
		case dirs["stillno"]:
			return nil
		}
		granted[a.Path] = true
		return nil
	}

	plan := mk()
	ensureAccess(plan, data, check, nil) // check-config: nothing changes
	if _, err := os.Stat(own); err == nil {
		t.Fatal("check only must not create the child's folder")
	}
	wantCheck := []string{"ok", "needs grant", "needs grant", "needs grant", "not granted: a system folder", "missing", "needs grant"}
	for i, a := range plan {
		if a.State != wantCheck[i] || a.refuses() {
			t.Fatalf("check only, %s: want %q, got %q", a.Path, wantCheck[i], a.State)
		}
	}

	plan = mk()
	ensureAccess(plan, data, check, grant)
	wantStart := []string{"ok", "granted", "denied: grant failed", "denied: still no access", "not granted", "missing", "granted"}
	for i, a := range plan {
		if !strings.HasPrefix(a.State, wantStart[i]) {
			t.Fatalf("start, %s: want %q, got %q", a.Path, wantStart[i], a.State)
		}
	}
	if fi, err := os.Stat(own); err != nil || !fi.IsDir() {
		t.Fatal("the child's own data folder must be created before the grant")
	}
}

// A path behind a junction is never granted (the grant would land on the link's target), nor is modify on a folder its
// user could swap for a link between the check and the grant.
func TestEnsureAccessNeverGrantsThroughLinks(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target")
	if err := os.MkdirAll(filepath.Join(target, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link")
	if out, err := exec.Command("cmd.exe", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatalf("mklink: %v %s", err, out)
	}
	userDir := filepath.Join(root, "userwrite")
	if err := os.Mkdir(userDir, 0o755); err != nil {
		t.Fatal(err)
	}
	plan := []ChildAccess{
		{Path: link, Need: needRead},
		{Path: filepath.Join(link, "sub"), Need: needRead},
		{Path: userDir, Need: needModify},
		{Path: target, Need: needRead},
	}
	denied := func(ChildAccess) error { return errors.New("Access is denied.") }
	var grantedPaths []string
	grant := func(a ChildAccess) error { grantedPaths = append(grantedPaths, a.Path); return nil }
	ensureAccess(plan, filepath.Join(root, "agent"), denied, grant)
	want := []string{"is a link", "is a link", "can change it or a folder above", ""}
	for i, a := range plan[:3] {
		if !strings.HasPrefix(a.State, "not granted: ") || !strings.Contains(a.State, want[i]) || a.refuses() {
			t.Fatalf("%s: want not granted (%s), got %q", a.Path, want[i], a.State)
		}
	}
	if len(grantedPaths) != 1 || grantedPaths[0] != target {
		t.Fatalf("only the real folder may be granted, got %v", grantedPaths)
	}
}

// grantLocalService leaves an inherited allow entry for LOCAL SERVICE on a folder, read-only outside the child's data.
func TestGrantLocalService(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "model.bin")
	if err := os.WriteFile(existing, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := grantLocalService(ChildAccess{Path: dir, Need: needRead, dir: true}); err != nil {
		t.Fatal(err)
	}
	if mask, _ := localServiceACEOf(t, existing, true); mask == 0 {
		t.Fatal("a file already in the folder must inherit the read entry")
	}
	mask, flags := localServiceACE(t, dir)
	const fileWriteData, inherit = 0x2, windows.OBJECT_INHERIT_ACE | windows.CONTAINER_INHERIT_ACE
	if mask == 0 || mask&fileWriteData != 0 || flags&inherit != inherit {
		t.Fatalf("want an inherited read entry for LOCAL SERVICE, got mask %#x flags %#x", mask, flags)
	}
	sub := filepath.Join(dir, "own")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := grantLocalService(ChildAccess{Path: sub, Need: needModify, dir: true}); err != nil {
		t.Fatal(err)
	}
	if mask, _ := localServiceACE(t, sub); mask&fileWriteData == 0 {
		t.Fatalf("modify must include writing, got mask %#x", mask)
	}
}

// localServiceACE: the explicit (not inherited) allow entry for LOCAL SERVICE on path.
func localServiceACE(t *testing.T, path string) (windows.ACCESS_MASK, uint8) {
	t.Helper()
	return localServiceACEOf(t, path, false)
}

func localServiceACEOf(t *testing.T, path string, inherited bool) (windows.ACCESS_MASK, uint8) {
	t.Helper()
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		t.Fatal(err)
	}
	for i := uint32(0); i < uint32(dacl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, i, &ace); err != nil {
			t.Fatal(err)
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if ace.Header.AceType == windows.ACCESS_ALLOWED_ACE_TYPE && (ace.Header.AceFlags&windows.INHERITED_ACE != 0) == inherited &&
			sid.String() == sidLocalService {
			return ace.Mask, ace.Header.AceFlags
		}
	}
	t.Fatalf("no LOCAL SERVICE entry (inherited %v) on %s", inherited, path)
	return 0, 0
}

// accessAs really runs under the other token: a folder only administrators may read is denied to the restricted token
// (administrators deny-only), and readable once everyone may read it; the calling thread keeps its own identity.
func TestAccessAsImpersonates(t *testing.T) {
	var self windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &self); err != nil {
		t.Fatal(err)
	}
	elevated := self.IsElevated()
	self.Close()
	if !elevated {
		t.Skip("needs an elevated test process (the Windows CI runner is)")
	}
	tok, err := probeToken("restricted")
	if err != nil {
		t.Fatal(err)
	}
	defer tok.Close()
	dir := filepath.Join(t.TempDir(), "admins-only")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("icacls.exe", dir, "/inheritance:r", "/grant", "*S-1-5-32-544:(OI)(CI)F",
		"/grant", "*S-1-5-18:(OI)(CI)F").CombinedOutput(); err != nil {
		t.Fatalf("icacls: %v %s", err, out)
	}
	a := ChildAccess{Path: dir, Need: needRead, dir: true}
	if err := accessAs(tok, a); err == nil {
		t.Fatal("the restricted token must not list a folder only administrators may read")
	}
	if _, err := os.ReadDir(dir); err != nil {
		t.Fatalf("the test process itself must still read it (identity reverted): %v", err)
	}
	if out, err := exec.Command("icacls.exe", dir, "/grant", "*S-1-1-0:(OI)(CI)RX").CombinedOutput(); err != nil {
		t.Fatalf("icacls: %v %s", err, out)
	}
	if err := accessAs(tok, a); err != nil {
		t.Fatalf("readable for everyone, so for the restricted token too: %v", err)
	}
	if err := accessAs(tok, ChildAccess{Path: dir, Need: needModify, dir: true}); err == nil {
		t.Fatal("read for everyone must not allow the restricted token to write")
	}
}
