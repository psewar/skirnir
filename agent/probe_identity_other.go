//go:build !windows

package main

import (
	"fmt"
	"io"
)

// runProbeIdentity: the measurement of ADR-0087 concerns the Windows agent (LocalSystem and its children). On Linux the
// children run as root under the unit's restrictions (ADR-0084); a probe for that is not part of this verb.
func runProbeIdentity(cfg *Config, cfgPath string, o ProbeOptions, out io.Writer) error {
	return fmt.Errorf("probe-identity measures the Windows agent (ADR-0087); not available on this platform")
}
