//go:build !windows

package main

// procPrivateBytes: not measured outside Windows (the OpenAI-compatible upstream with a standby runs on the Windows
// desktop nodes); the RAM check before a release then has no need to compare and lets the release through.
func procPrivateBytes(pid uint32) (uint64, bool) { return 0, false }
