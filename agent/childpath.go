package main

import (
	"fmt"
	"io"
	"os/exec"
	"path/filepath"
)

// Children run with the agent's rights: LocalSystem on Windows, root on Linux. If someone else may change the program a
// child starts - the file, the folder it loads its libraries from, or a folder above that could be swapped - that
// someone runs code with those rights on the next start (TM-14, ADR-0085). A per-user Ollama install under
// %LOCALAPPDATA% is the common case: the user owns the folder.
//
// childExposure resolves the child's command and asks the platform (pathExposure) who besides the system, the
// administrators and the installer service could change it. The supervisor then refuses to start the child, unless
// children[].user_writable_reason states why it is accepted; check-config reports the same before an update swaps
// the binary.

type exposureKind int

const (
	exposeFile     exposureKind = iota // the program: write, delete, change permissions or owner
	exposeDir                          // the folder holding it: also add files (libraries, a renamed program)
	exposeAncestor                     // a folder above: remove or rename its entries, change permissions or owner
)

// ChildExposure: the first path of a child's program that someone else could change, and who.
type ChildExposure struct {
	Child  string `json:"child"`
	Path   string `json:"path"`
	Who    string `json:"who"`
	Reason string `json:"reason,omitempty"` // children[].user_writable_reason: accepted, child starts
}

func (e ChildExposure) String() string {
	return fmt.Sprintf("child %s: %s can be changed by %s", e.Child, e.Path, e.Who)
}

// childExposure returns nil when nobody else could change the program, or when the command cannot be resolved (the
// start then fails on its own and says why).
func childExposure(sp ChildSpec) (*ChildExposure, error) {
	exe, err := exec.LookPath(sp.Cmd)
	if err != nil {
		return nil, nil // exception-ok: not found is reported by the start itself; nothing to judge here
	}
	if exe, err = filepath.Abs(exe); err != nil {
		return nil, err
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		exe = real
	}
	steps := []struct {
		path string
		kind exposureKind
	}{{exe, exposeFile}, {filepath.Dir(exe), exposeDir}}
	for p := filepath.Dir(exe); filepath.Dir(p) != p; p = filepath.Dir(p) {
		steps = append(steps, struct {
			path string
			kind exposureKind
		}{filepath.Dir(p), exposeAncestor})
	}
	for _, s := range steps {
		who, err := pathExposure(s.path, s.kind)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", s.path, err)
		}
		if who != "" {
			return &ChildExposure{Child: sp.Name, Path: s.path, Who: who, Reason: sp.UserWritableReason}, nil
		}
	}
	return nil, nil
}

// reportChildExposures prints one line per child whose program someone else could change, for check-config: "accepted:"
// with its user_writable_reason, or "refused:" with the next step. An error when any child would be refused, so the
// self-update (which runs check-config of the new binary first) keeps the running version instead of swapping to one
// that would not start the node's Ollama.
func reportChildExposures(specs []ChildSpec, w io.Writer) error {
	exps, errs := childExposures(specs)
	refused := len(errs)
	for _, e := range exps {
		if e.Reason != "" {
			fmt.Fprintf(w, "accepted: %s (user_writable_reason: %s)\n", e, e.Reason)
			continue
		}
		refused++
		fmt.Fprintf(w, "refused: %s; next: install it in a folder only administrators can change, or state why it is "+
			"accepted in children[].user_writable_reason (ADR-0085)\n", e)
	}
	for _, err := range errs {
		fmt.Fprintf(w, "refused: permission check failed: %v\n", err)
	}
	if refused > 0 {
		return fmt.Errorf("%d child(ren) would not start", refused)
	}
	return nil
}

// childExposures checks every enabled child; for check-config and the start.
func childExposures(specs []ChildSpec) ([]ChildExposure, []error) {
	var out []ChildExposure
	var errs []error
	for _, sp := range specs {
		if !sp.on() {
			continue
		}
		e, err := childExposure(sp)
		if err != nil {
			errs = append(errs, fmt.Errorf("child %s: %w", sp.Name, err))
			continue
		}
		if e != nil {
			out = append(out, *e)
		}
	}
	return out, errs
}
