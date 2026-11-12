//go:build windows

package main

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

const sidLocalService = "S-1-5-19" // NT AUTHORITY\LOCAL SERVICE

var (
	identAdvapi = windows.NewLazySystemDLL("advapi32.dll")
	pLogonUserW = identAdvapi.NewProc("LogonUserW")
)

const (
	logon32LogonService    = 5
	logon32ProviderDefault = 0
)

// localServiceToken logs on NT AUTHORITY\LOCAL SERVICE: a built-in account, no password, nothing to create. Only
// LocalSystem may do this. The caller closes the token.
func localServiceToken() (windows.Token, error) {
	var tok windows.Token
	r, _, e := pLogonUserW.Call(uintptr(unsafe.Pointer(windows.StringToUTF16Ptr("LocalService"))),
		uintptr(unsafe.Pointer(windows.StringToUTF16Ptr("NT AUTHORITY"))), 0, logon32LogonService, logon32ProviderDefault,
		uintptr(unsafe.Pointer(&tok)))
	if r == 0 {
		return 0, fmt.Errorf("LogonUser LocalService: %v", e)
	}
	return tok, nil
}

// runningAsSystem: this process runs as LocalSystem (the service, the self-update's check-config).
func runningAsSystem() bool {
	var t windows.Token
	if windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &t) != nil {
		return false
	}
	defer t.Close()
	u, err := t.GetTokenUser()
	return err == nil && u.User.Sid.String() == "S-1-5-18"
}

// prepareChildLaunch: the token, environment and folder access for one start (ADR-0088). With an error the child is
// refused; the returned launch still carries the access found, for the status.
func prepareChildLaunch(sp ChildSpec, dataDir string) (*childLaunch, error) {
	who := childRunAs(sp, "windows")
	if who == runAsSystem {
		return &childLaunch{runAs: who, attr: childProcAttr(), env: os.Environ(), release: func() {}}, nil
	}
	l := &childLaunch{runAs: who, release: func() {}}
	tok, err := localServiceToken()
	if err != nil {
		return l, err
	}
	l.access = childAccessPlan(sp, dataDir)
	ensureAccess(l.access, dataDir, func(a ChildAccess) error { return accessAs(tok, a) }, grantLocalService)
	markResolve(l.access, func(p string) string { return resolveAs(tok, p) })
	for _, a := range l.access {
		if a.refuses() {
			tok.Close()
			return l, fmt.Errorf("%s access to %s: %s", a.Need, a.Path, a.State)
		}
	}
	env, err := tok.Environ(false) // the account's own profile and variables, as a service under it would get them
	if err != nil {
		tok.Close()
		return l, fmt.Errorf("environment of LOCAL SERVICE: %w", err)
	}
	l.env = env
	l.attr = &syscall.SysProcAttr{Token: syscall.Token(tok), HideWindow: true, CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
	l.release = func() { tok.Close() }
	return l, nil
}

// checkChildIdentity: what check-config reports. Checks access as LOCAL SERVICE but grants nothing; only LocalSystem can
// log on that account, so from an administrator's console the access stays "not checked" (said as a note, not an error).
func checkChildIdentity(sp ChildSpec, dataDir string) (string, []ChildAccess, string, error) {
	who := childRunAs(sp, "windows")
	if who == runAsSystem {
		return who, nil, "", nil
	}
	plan := childAccessPlan(sp, dataDir)
	if !runningAsSystem() {
		for i := range plan {
			plan[i].State = "not checked"
		}
		return who, plan, "access is checked only as LocalSystem (the service, the self-update's check-config)", nil
	}
	tok, err := localServiceToken()
	if err != nil {
		return who, nil, "", err
	}
	defer tok.Close()
	ensureAccess(plan, dataDir, func(a ChildAccess) error { return accessAs(tok, a) }, nil)
	markResolve(plan, func(p string) string { return resolveAs(tok, p) })
	return who, plan, "", nil
}

// childAccessPlan derives what the child needs: the program's folder, cwd, every env value and argument that is a single
// absolute path, read_paths and write_paths. Paths in the children's data folder (or, from before ADR-0091, the agent's
// own folder) become modify on the child's own folder there (the first level below it); everything else is read. Parents come before their contents, so a folder's inherited
// grant is seen before its subfolders are checked.
func childAccessPlan(sp ChildSpec, dataDir string) []ChildAccess {
	type cand struct{ path, from, need string }
	var cands []cand
	if exe, err := exec.LookPath(sp.Cmd); err == nil {
		if abs, err := filepath.Abs(exe); err == nil {
			cands = append(cands, cand{filepath.Dir(abs), "program", ""})
		}
	}
	if sp.Cwd != "" {
		cands = append(cands, cand{sp.Cwd, "cwd", ""})
	}
	for _, e := range sp.Env {
		if k, v, ok := strings.Cut(e, "="); ok && singlePath(v) {
			cands = append(cands, cand{v, "env " + k, ""})
		}
	}
	for _, a := range sp.Args {
		v := a
		if strings.HasPrefix(a, "-") {
			_, after, ok := strings.Cut(a, "=")
			if !ok {
				continue
			}
			v = after
		}
		if singlePath(v) {
			cands = append(cands, cand{v, "arg", ""})
		}
	}
	for _, p := range sp.ReadPaths {
		cands = append(cands, cand{p, "read_paths", needRead})
	}
	for _, p := range sp.WritePaths {
		cands = append(cands, cand{p, "write_paths", needModify})
	}
	var out []ChildAccess
	at := map[string]int{}
	for _, c := range cands {
		a := classifyAccess(c.path, c.from, c.need, dataDir)
		k := strings.ToLower(a.Path)
		if i, ok := at[k]; ok {
			if out[i].Need == needRead && a.Need == needModify {
				out[i] = a
			}
			continue
		}
		at[k] = len(out)
		out = append(out, a)
	}
	sort.SliceStable(out, func(i, j int) bool { return len(out[i].Path) < len(out[j].Path) })
	return out
}

func singlePath(v string) bool { return winAbsPath.MatchString(v) && !strings.Contains(v, ";") }

// underDir: rel is p relative to base ("" for base itself); false when p is not inside base.
func underDir(p, base string) (string, bool) {
	if strings.EqualFold(p, base) {
		return "", true
	}
	prefix := strings.TrimRight(base, `\`) + `\`
	if len(p) > len(prefix) && strings.EqualFold(p[:len(prefix)], prefix) {
		return p[len(prefix):], true
	}
	return "", false
}

// classifyAccess: in the children's data folder (ADR-0091) or, from before, the agent's own folder, a path becomes modify
// on the child's own folder there (the first level); anything else is read, never granted when too broad.
func classifyAccess(path, from, need, dataDir string) ChildAccess {
	p := filepath.Clean(path)
	a := ChildAccess{Path: p, From: from, Need: needRead}
	if need == needModify {
		a.Need = needModify
	}
	root, data := childDataRoot(dataDir), filepath.Clean(dataDir)
	for _, base := range []string{root, data} {
		rel, ok := underDir(p, base)
		if !ok {
			continue
		}
		first, _, nested := strings.Cut(rel, `\`)
		// directly in a data folder only a folder (existing, or missing and without a file extension) is the child's own;
		// a file in the agent's folder is the agent's (config.yaml, keys)
		fi, err := os.Stat(p)
		isFile := (err == nil && !fi.IsDir()) || (err != nil && filepath.Ext(p) != "")
		if rel == "" || (!nested && isFile) {
			a.noGrant = "the agent's own folder (configuration, keys)"
			if base == root {
				a.noGrant = "the children's data folder itself; name the child's own folder in it"
			}
			return a
		}
		a.Path = filepath.Join(base, first)
		if need == "" {
			a.Need = needModify
		}
		a.ownData = true
		a.inRoot = base == root
		if base == data {
			a.MoveTo = filepath.Join(root, first)
		}
		return a
	}
	a.noGrant = tooBroad(p)
	return a
}

// tooBroad: a folder whose grant would open far more than one child needs. Such a path is only checked, never granted.
func tooBroad(p string) string {
	if filepath.Dir(p) == p {
		return "a drive root; name a narrower folder in read_paths"
	}
	users := filepath.Join(os.Getenv("SystemDrive")+`\`, "Users")
	for _, sys := range []string{os.Getenv("SystemRoot"), os.Getenv("ProgramFiles"), os.Getenv("ProgramFiles(x86)"),
		os.Getenv("ProgramData"), users} {
		if sys != "" && strings.EqualFold(p, filepath.Clean(sys)) {
			return "a system folder; name a narrower folder in read_paths"
		}
	}
	if strings.EqualFold(filepath.Dir(p), users) {
		return "a whole user profile; name a narrower folder in read_paths"
	}
	return ""
}

// ensureAccess fills in each entry's state: checks it as the child's account, grants what is missing (grant nil = only
// check, for check-config) and checks again. The child's own data folder is created when it does not exist yet.
func ensureAccess(plan []ChildAccess, dataDir string, check func(ChildAccess) error, grant func(ChildAccess) error) {
	root, rootIssue, rootSeen := childDataRoot(dataDir), "", false
	for i := range plan {
		a := &plan[i]
		if a.inRoot {
			if !rootSeen {
				rootIssue, rootSeen = ensureChildRoot(root, grant != nil), true
			}
			if rootIssue != "" {
				a.State = "denied: the children's data folder " + root + " " + rootIssue
				continue
			}
		}
		fi, err := os.Stat(a.Path)
		if err != nil && a.ownData {
			if grant == nil {
				a.State = "needs grant"
				continue
			}
			if err := os.MkdirAll(a.Path, 0o755); err != nil {
				a.State = "denied: cannot create the child's data folder: " + err.Error()
				continue
			}
			fi, err = os.Stat(a.Path)
		}
		if err != nil {
			a.State = "missing"
			continue
		}
		a.dir = fi.IsDir()
		if check(*a) == nil {
			a.State = "ok"
			continue
		}
		if a.noGrant == "" {
			a.noGrant = redirected(a.Path)
		}
		if a.noGrant == "" && a.Need == needModify && !a.ownData {
			a.noGrant = changeableByOthers(*a)
		}
		if a.noGrant != "" {
			a.State = "not granted: " + a.noGrant
			continue
		}
		if grant == nil {
			a.State = "needs grant"
			continue
		}
		if err := grant(*a); err != nil {
			a.State = "denied: grant failed: " + err.Error()
			continue
		}
		if err := check(*a); err != nil {
			a.State = "denied: still no access after the grant: " + err.Error()
			continue
		}
		a.State = "granted"
	}
}

// childRootSDDL: the children's data folder belongs to the system and the administrators; LOCAL SERVICE may only list
// this folder (no inheritance: each child's own folder gets its modify entry from the grant), so a child can resolve the
// paths below it (ADR-0091). Protected: nothing is inherited from ProgramData, where users may create folders.
const childRootSDDL = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;;0x1200a9;;;LS)"

// ensureChildRoot: the children's data folder exists (created with childRootSDDL when create is set and it is missing),
// is no link, and nobody besides the system and the administrators can change it or a folder above. Returns why not,
// "" when it is fine or (check only) still missing - the agent creates it at the next start.
func ensureChildRoot(root string, create bool) string {
	if _, err := os.Lstat(root); err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			return "cannot be checked: " + err.Error()
		}
		if !create {
			return ""
		}
		sd, err := windows.SecurityDescriptorFromString(childRootSDDL)
		if err != nil {
			return "cannot be created: " + err.Error()
		}
		sa := &windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), SecurityDescriptor: sd}
		// fails when someone created it between the look above and here; the checks below then judge that folder
		if err := windows.CreateDirectory(windows.StringToUTF16Ptr(root), sa); err != nil && !errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
			return "cannot be created: " + err.Error()
		}
	}
	if r := redirected(root); r != "" {
		return r
	}
	if who := changeableByOthers(ChildAccess{Path: root, dir: true}); who != "" {
		return "is not the system's and administrators' only: " + who + "; remove the folder, the agent creates it anew"
	}
	return ""
}

// resolveAs: can the token resolve p to its full form, as filepath.EvalSymlinks does (every folder above p is listed)?
// The real call under the token is the check; when it fails, the first folder above p it cannot list is named.
func resolveAs(tok windows.Token, p string) string {
	out := ""
	err := asToken(tok, func() error {
		_, rerr := filepath.EvalSymlinks(p)
		if rerr == nil {
			out = "ok"
			return nil
		}
		for q := filepath.Dir(filepath.Clean(p)); ; q = filepath.Dir(q) {
			if !listable(q) {
				out = q + " cannot be listed by the child; programs that resolve paths (Go's filepath.EvalSymlinks, Ollama) fail here"
				return nil
			}
			if filepath.Dir(q) == q {
				break
			}
		}
		out = "cannot be resolved: " + rerr.Error()
		return nil
	})
	if err != nil {
		return "not checked: " + err.Error()
	}
	return out
}

func listable(dir string) bool {
	f, err := os.Open(dir)
	if err != nil {
		return false
	}
	defer f.Close()
	_, err = f.Readdirnames(1)
	return err == nil || err == io.EOF
}

// markResolve fills in Resolve for every entry the child has access to.
func markResolve(plan []ChildAccess, resolve func(string) string) {
	for i := range plan {
		if plan[i].State == "ok" || plan[i].State == "granted" {
			plan[i].Resolve = resolve(plan[i].Path)
		}
	}
}

// accessAs checks one entry with the token's rights: read = open the file or list the folder; modify = create and remove
// a file in the folder (or open the file for writing).
func accessAs(tok windows.Token, a ChildAccess) error {
	return asToken(tok, func() error {
		if a.Need == needModify {
			if !a.dir {
				f, err := os.OpenFile(a.Path, os.O_WRONLY, 0)
				if err != nil {
					return err
				}
				return f.Close()
			}
			f, err := os.CreateTemp(a.Path, ".skirnir-access-*")
			if err != nil {
				return err
			}
			name := f.Name()
			f.Close()
			return os.Remove(name)
		}
		f, err := os.Open(a.Path)
		if err != nil {
			return err
		}
		defer f.Close()
		if a.dir {
			if _, err := f.Readdirnames(1); err != nil && err != io.EOF { // EOF: an empty folder, listed fine
				return err
			}
		}
		return nil
	})
}

// asToken runs f on a thread impersonating tok. It runs on a goroutine of its own, locked to its thread: if reverting
// failed, that thread ends with the goroutine instead of going back to the pool with the other identity.
func asToken(tok windows.Token, f func() error) error {
	var imp windows.Token
	if err := windows.DuplicateTokenEx(tok, windows.TOKEN_IMPERSONATE|windows.TOKEN_QUERY, nil, windows.SecurityImpersonation,
		windows.TokenImpersonation, &imp); err != nil {
		return fmt.Errorf("DuplicateTokenEx: %w", err)
	}
	defer imp.Close()
	res := make(chan error, 1)
	go func() {
		runtime.LockOSThread()
		if err := windows.SetThreadToken(nil, imp); err != nil {
			runtime.UnlockOSThread()
			res <- fmt.Errorf("SetThreadToken: %w", err)
			return
		}
		ferr := f()
		if err := windows.RevertToSelf(); err != nil {
			res <- fmt.Errorf("RevertToSelf: %w", err) // the thread stays locked and ends with this goroutine
			return
		}
		runtime.UnlockOSThread()
		res <- ferr
	}()
	return <-res
}

// redirected: a path that a junction or symbolic link leads elsewhere is never granted. Whoever may replace a configured
// folder with a link (a model folder in a user's profile) would otherwise choose where the agent, as LocalSystem, grants
// LOCAL SERVICE access - for modify, a program folder.
// The path and every folder above it are looked at themselves (reparse point attribute), so a short 8.3 name is not
// mistaken for a link.
func redirected(p string) string {
	for q := filepath.Clean(p); ; q = filepath.Dir(q) {
		fi, err := os.Lstat(q)
		if err != nil {
			return "cannot be checked for links: " + err.Error()
		}
		if d, ok := fi.Sys().(*syscall.Win32FileAttributeData); ok && d.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
			return q + " is a link (junction, symbolic link or cloud folder); name the folder it leads to instead"
		}
		if filepath.Dir(q) == q {
			return ""
		}
	}
}

// changeableByOthers: a modify grant outside the child's own data folder only where nobody besides the system and the
// administrators can change the path or a folder above it (ADR-0085 rules). Otherwise that someone could swap the folder
// for a link between the check and the grant and so pick what LOCAL SERVICE may change - a program folder, for example.
func changeableByOthers(a ChildAccess) string {
	kind := exposeDir
	if !a.dir {
		kind = exposeFile
	}
	who, err := pathExposure(a.Path, kind)
	for q := a.Path; err == nil && who == "" && filepath.Dir(q) != q; q = filepath.Dir(q) {
		who, err = pathExposure(filepath.Dir(q), exposeAncestor)
	}
	switch {
	case err != nil:
		return "its permissions cannot be read: " + err.Error()
	case who != "":
		return who + " can change it or a folder above; a modify grant there could be redirected"
	}
	return ""
}

// grantLocalService adds an allow entry for LOCAL SERVICE: read and execute, or modify on the child's own data folder;
// on folders inherited by everything below. /L: on the named path itself, never on the target of a link that replaced
// it after the check above.
func grantLocalService(a ChildAccess) error {
	perm := "(RX)"
	if a.Need == needModify {
		perm = "(M)"
	}
	if a.dir {
		perm = "(OI)(CI)" + perm
	}
	if out, err := exec.Command("icacls.exe", a.Path, "/grant", "*"+sidLocalService+":"+perm, "/L").CombinedOutput(); err != nil {
		return fmt.Errorf("icacls: %v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}
