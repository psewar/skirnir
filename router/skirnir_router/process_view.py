"""What the running router could change on disk, from its own point of view (TM-14, ADR-0081).

The systemd unit decides what the router may write: `ProtectSystem=strict` mounts the system read-only,
`ReadWritePaths=` and `ReadOnlyPaths=` add bind mounts, and the empty capability bounding set leaves owner and mode as
the only permission check. Whether a configured file is safe from the router therefore depends on the mount namespace
and the credentials of the running process, not on the configuration. `ProcessView.current()` reads both from
`/proc/self` (Linux only; elsewhere it reports None), and `changeable(path)` says whether this process could change the
file, or put another file or folder in its place:

* the file itself lies on a writable mount and owner or mode let this process write it;
* the folder holding it, or any folder above it, lies on a writable mount and lets this process add or remove entries
  (a sticky folder only for entries it owns); a folder that is itself a mount point cannot be renamed or removed.

Only the running router has the unit's view. `router.py --posture` runs outside the unit, where root is writable
everywhere, so it does not use this check.
"""
import os
import posixpath
import re

CAP_DAC_OVERRIDE = 1
CAP_FOWNER = 3
_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(field):
    """mountinfo writes space, tab, newline and backslash in paths as octal escapes."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text):
    """((mount point, read_only), ...) in mount order from the text of /proc/self/mountinfo."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if " - " not in line or len(fields) < 6:
            continue
        super_opts = line.split(" - ", 1)[1].split()
        read_only = "ro" in fields[5].split(",") or (len(super_opts) > 2 and "ro" in super_opts[2].split(","))
        mounts.append((_unescape(fields[4]), read_only))
    return tuple(mounts)


def _within(path, mount_point):
    return mount_point == "/" or path == mount_point or path.startswith(mount_point.rstrip("/") + "/")


class ProcessView:
    def __init__(self, mounts, euid, groups, caps, lstat=os.lstat, stat=os.stat, exists=os.path.lexists,
                 realpath=os.path.realpath):
        self.mounts = tuple(mounts)
        self.mount_points = {m for m, _ in self.mounts}
        self.euid = euid
        self.groups = frozenset(groups)
        self.dac_override = bool(caps >> CAP_DAC_OVERRIDE & 1)
        self.fowner = bool(caps >> CAP_FOWNER & 1)
        self._lstat, self._stat, self._exists, self._realpath = lstat, stat, exists, realpath

    @classmethod
    def current(cls):
        """The view of this process, or None where /proc/self does not describe it (not Linux)."""
        if not (os.path.isfile("/proc/self/mountinfo") and os.path.isfile("/proc/self/status")):
            return None
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            mounts = parse_mountinfo(f.read())
        caps = 0
        with open("/proc/self/status", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    caps = int(line.split()[1], 16)
        return cls(mounts, os.geteuid(), {os.getegid(), *os.getgroups()}, caps)

    def read_only(self, path):
        """Whether the mount that holds path is read-only (the last of the longest matching mount points wins)."""
        best, ro = "", False
        for mount_point, read_only in self.mounts:
            if _within(path, mount_point) and len(mount_point) >= len(best):
                best, ro = mount_point, read_only
        return ro

    def _may_write(self, st):
        """Owner and mode only: the unit leaves the router no capability that would bypass them. Called for objects on
        a writable mount, where the owner may always grant itself write access (chmod needs ownership, not the bit)."""
        if self.dac_override or self.fowner or st.st_uid == self.euid:
            return True
        if st.st_gid in self.groups:
            return bool(st.st_mode & 0o020)
        return bool(st.st_mode & 0o002)

    def _may_replace_entry(self, folder, entry):
        """Whether this process could remove or rename entry in folder and put another in its place. The folder is
        judged where it really lies (a symlinked folder by its target), the entry by its own name in that folder."""
        real_folder = self._realpath(folder)
        real_entry = posixpath.join(real_folder, posixpath.basename(entry))
        if real_entry in self.mount_points or self.read_only(real_folder):
            return False
        st = self._stat(real_folder)
        if not self._may_write(st):
            return False
        if st.st_mode & 0o1000 and not self.fowner:   # sticky: only the owner of the entry or of the folder
            return self._lstat(real_entry).st_uid == self.euid or st.st_uid == self.euid
        return True

    def changeable(self, path):
        """Whether this process could change the file at path or replace it; False for a path that does not exist."""
        lexical = posixpath.normpath(path)
        if not self._exists(lexical):
            return False
        real = self._realpath(lexical)
        # A dangling link, or a target this process cannot reach, has no file to change; the link itself still counts.
        real_exists = self._exists(real)
        if real_exists and not self.read_only(real) and self._may_write(self._stat(real)):
            return True
        for start in ({lexical, real} if real_exists else {lexical}):
            entry = start
            while entry != "/":
                folder = posixpath.dirname(entry)
                if self._may_replace_entry(folder, entry):
                    return True
                entry = folder
        return False
