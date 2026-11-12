package main

import (
	"fmt"
	"io"
	"regexp"
	"strings"
	"syscall"
)

// ADR-0088: on Windows the children (Ollama, the STT service, Strata) run as NT AUTHORITY\LOCAL SERVICE instead of the
// agent's LocalSystem. They are the large code that handles network input (prompts, tool calls, audio); a bug in one of
// them no longer yields SYSTEM. The agent keeps LocalSystem for its own privileged functions (GPU guard, game detection,
// updates; ADR-0087 option 2 later).
//
// Before every start the agent derives the folders the child needs from its command line, cwd and env (plus read_paths and
// write_paths), checks each one as LOCAL SERVICE and grants what is missing: read on folders outside the agent's data
// folder, modify on the child's own folder under it. The result is a fact in the status (children[].access) and in
// check-config. A child whose access cannot be established is refused like an exposed program (ADR-0085), never started
// with more rights instead.

const (
	runAsLocalService = "localservice"
	runAsSystem       = "system"
	runAsAgent        = "agent" // other platforms: the agent's own account

	needRead   = "read"
	needModify = "modify"
)

// ChildAccess is one folder or file a LOCAL SERVICE child needs, and what the agent found or did.
type ChildAccess struct {
	Path  string `json:"path"`
	Need  string `json:"need"`  // read | modify
	From  string `json:"from"`  // program | cwd | env NAME | arg | read_paths | write_paths
	State string `json:"state"` // ok | granted | needs grant | not checked | missing | not granted: <why> | denied: <why>

	noGrant string // set: only checked, never granted (too broad, or the agent's own files)
	ownData bool   // the child's own folder under the agent's data folder (created when missing)
	dir     bool
}

func (a ChildAccess) refuses() bool { return strings.HasPrefix(a.State, "denied") }

// childLaunch is what the platform hands the supervisor for one start: the process attributes (the token on Windows),
// the identity's own base environment (the child's env is appended) and what to release after the start.
type childLaunch struct {
	runAs   string
	attr    *syscall.SysProcAttr
	env     []string
	access  []ChildAccess
	release func()
}

// childRunAs: the identity a child runs as on the given platform.
func childRunAs(sp ChildSpec, goos string) string {
	if goos != "windows" {
		return runAsAgent
	}
	if sp.RunAs == runAsSystem {
		return runAsSystem
	}
	return runAsLocalService
}

var winAbsPath = regexp.MustCompile(`^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)`)

// validateRunAs: run_as is localservice (default) or system with a reason; the keys exist only on Windows.
func validateRunAs(sp ChildSpec, goos string) error {
	switch sp.RunAs {
	case "", runAsLocalService:
		if sp.RunAsReason != "" {
			return fmt.Errorf("children: %s: run_as_reason is only for run_as: system", sp.Name)
		}
	case runAsSystem:
		if strings.TrimSpace(sp.RunAsReason) == "" {
			return fmt.Errorf("children: %s: run_as: system needs run_as_reason - the child would run with the agent's "+
				"LocalSystem rights (ADR-0088)", sp.Name)
		}
	default:
		return fmt.Errorf("children: %s: run_as %q is not localservice or system", sp.Name, sp.RunAs)
	}
	if goos != "windows" {
		if sp.RunAs != "" || len(sp.ReadPaths) > 0 || len(sp.WritePaths) > 0 {
			return fmt.Errorf("children: %s: run_as, read_paths and write_paths apply only on Windows; here children run "+
				"as the agent's own account (ADR-0084)", sp.Name)
		}
		return nil
	}
	for _, p := range append(append([]string(nil), sp.ReadPaths...), sp.WritePaths...) {
		if !winAbsPath.MatchString(p) {
			return fmt.Errorf("children: %s: read_paths/write_paths entry %q is not an absolute path", sp.Name, p)
		}
	}
	return nil
}

// reportChildIdentities prints, for check-config, the identity of every enabled child and its access as checked now (no
// change). An error only when a child could not be started under its identity at all; missing grants are not an error,
// the agent sets them before the start.
func reportChildIdentities(specs []ChildSpec, dataDir string, w io.Writer) error {
	refused := 0
	for _, sp := range specs {
		if !sp.on() {
			continue
		}
		who, access, note, err := checkChildIdentity(sp, dataDir)
		if err != nil {
			refused++
			fmt.Fprintf(w, "refused: child %s runs as %s, but: %v\n", sp.Name, who, err)
			continue
		}
		line := fmt.Sprintf("identity: child %s runs as %s", sp.Name, who)
		if who == runAsSystem {
			line += " (run_as_reason: " + sp.RunAsReason + ")"
		}
		if note != "" {
			line += "; " + note
		}
		fmt.Fprintln(w, line)
		for _, a := range access {
			if a.refuses() {
				refused++
			}
			fmt.Fprintf(w, "  access %-6s %-11s %s (%s)\n", a.Need, a.State, a.Path, a.From)
		}
	}
	if refused > 0 {
		return fmt.Errorf("%d child(ren) could not start under their identity", refused)
	}
	return nil
}
