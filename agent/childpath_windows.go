package main

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// pathExposure (Windows): who besides SYSTEM, Administrators and TrustedInstaller may change path in the way kind
// names, read from its owner and DACL. The owner counts because it may always rewrite the DACL. Inherit-only entries
// apply to children, not to path, and are skipped; deny entries are ignored, so the answer may name someone a deny entry
// would stop - never the other way round.

const (
	fileWriteData   = 0x2 // FILE_WRITE_DATA / FILE_ADD_FILE
	fileAppendData  = 0x4 // FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
	fileDeleteChild = 0x40
	accessDelete    = 0x10000
	accessWriteDAC  = 0x40000
	accessWriteOwn  = 0x80000
	genericAll      = 0x10000000
	genericWrite    = 0x40000000
)

var exposureMask = map[exposureKind]uint32{
	exposeFile:     fileWriteData | fileAppendData | accessDelete | accessWriteDAC | accessWriteOwn | genericAll | genericWrite,
	exposeDir:      fileWriteData | fileAppendData | fileDeleteChild | accessDelete | accessWriteDAC | accessWriteOwn | genericAll | genericWrite,
	exposeAncestor: fileDeleteChild | accessDelete | accessWriteDAC | accessWriteOwn | genericAll,
}

// trustedSIDs may change system programs anyway; CREATOR OWNER and OWNER RIGHTS stand for the owner, checked separately.
var trustedSIDs = map[string]bool{
	"S-1-5-18":     true, // LocalSystem
	"S-1-5-32-544": true, // Administrators
	"S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464": true, // TrustedInstaller
	"S-1-3-0": true, // CREATOR OWNER
	"S-1-3-4": true, // OWNER RIGHTS
}

func sidName(sid *windows.SID) string {
	if account, domain, _, err := sid.LookupAccount(""); err == nil && account != "" {
		if domain != "" {
			return domain + `\` + account
		}
		return account
	}
	return sid.String()
}

func pathExposure(path string, kind exposureKind) (string, error) {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return "", err
	}
	owner, _, err := sd.Owner()
	if err != nil {
		return "", err
	}
	if owner != nil && !trustedSIDs[owner.String()] {
		return sidName(owner) + " (owner)", nil
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return "", err
	}
	if dacl == nil {
		return "everyone (no access list)", nil
	}
	mask := exposureMask[kind]
	for i := uint32(0); i < uint32(dacl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, i, &ace); err != nil {
			return "", fmt.Errorf("access list entry %d: %w", i, err)
		}
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE || ace.Header.AceFlags&windows.INHERIT_ONLY_ACE != 0 {
			continue
		}
		if uint32(ace.Mask)&mask == 0 {
			continue
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if !trustedSIDs[sid.String()] {
			return sidName(sid), nil
		}
	}
	return "", nil
}
