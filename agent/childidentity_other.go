//go:build !windows

package main

import "os"

// Other platforms: children run as the agent's own account (ADR-0084: a hardened unit, no root); run_as does not apply.

func prepareChildLaunch(sp ChildSpec, dataDir string) (*childLaunch, error) {
	return &childLaunch{runAs: runAsAgent, attr: childProcAttr(), env: os.Environ(), release: func() {}}, nil
}

func checkChildIdentity(sp ChildSpec, dataDir string) (string, []ChildAccess, string, error) {
	return runAsAgent, nil, "", nil
}
