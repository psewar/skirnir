#!/usr/bin/env python3
"""Self-test (TM-14, ADR-0083): the running router reports TLS files it could change or replace.

process_view.ProcessView decides from the mount table and the credentials of the process whether a file could be
changed or replaced. The cases mirror the router unit: ProtectSystem=strict (root read-only), ReadWritePaths for
/etc/skirnir-router, ReadOnlyPaths for its certs and tls folders (ADR-0073, ADR-0082), root without capabilities. A
certificate in certs or tls is safe; one in another folder below /etc/skirnir-router (here: copy), or one the process
owns, is not. Sticky folders, symlinks, mount points,
mountinfo escapes and the posture finding are checked too, and on Linux the view of this very process. Synthetic paths
only."""
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import posture  # noqa: E402
from skirnir_router.process_view import ProcessView, parse_mountinfo  # noqa: E402

MOUNTINFO = (
    "24 1 8:1 / / ro,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "25 24 8:1 /etc/skirnir-router /etc/skirnir-router rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "26 25 8:1 /etc/skirnir-router/certs /etc/skirnir-router/certs ro,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "29 25 8:1 /etc/skirnir-router/tls /etc/skirnir-router/tls ro,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "27 24 0:5 / /var/lib/skirnir\\040router rw,nosuid - tmpfs tmpfs rw\n"
    "28 24 0:6 / /srv/readonly-fs rw - ext4 /dev/sdb1 ro\n"
)


def st(uid=0, gid=0, mode=0o644):
    return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)


class FakeFs:
    """Paths -> stat results; symlinks as a separate map. Directories get mode 0o755 unless named otherwise."""

    def __init__(self, entries, links=None):
        self.entries = dict(entries)
        self.links = dict(links or {})

    def realpath(self, path):
        for link, target in self.links.items():
            if path == link or path.startswith(link + "/"):
                return target + path[len(link):]
        return path

    def stat(self, path):
        return self.entries[self.realpath(path)]

    def lstat(self, path):
        return st(mode=0o777) if path in self.links else self.entries[path]

    def exists(self, path):
        return path in self.links or self.realpath(path) in self.entries


def view(fs, mounts=MOUNTINFO, euid=0, groups=(0,), caps=0, cwd="/"):
    return ProcessView(parse_mountinfo(mounts), euid, groups, caps, lstat=fs.lstat, stat=fs.stat, exists=fs.exists,
                       realpath=fs.realpath, getcwd=lambda: cwd)


def router_fs(**extra):
    entries = {"/": st(mode=0o755), "/etc": st(mode=0o755), "/etc/skirnir-router": st(mode=0o700),
               "/etc/skirnir-router/certs": st(mode=0o755), "/etc/skirnir-router/certs/fullchain.pem": st(),
               "/etc/skirnir-router/certs/privkey.pem": st(mode=0o600), "/etc/skirnir-router/tls": st(mode=0o700),
               "/etc/skirnir-router/tls/server.crt": st(), "/etc/skirnir-router/tls/server.key": st(mode=0o600),
               "/etc/skirnir-router/copy": st(mode=0o700),
               "/etc/skirnir-router/copy/server.crt": st(), "/etc/skirnir-router/copy/server.key": st(mode=0o600)}
    entries.update(extra)
    return FakeFs(entries)


class MountinfoTest(unittest.TestCase):
    def test_parse(self):
        mounts = dict(parse_mountinfo(MOUNTINFO))
        self.assertTrue(mounts["/"])
        self.assertFalse(mounts["/etc/skirnir-router"])
        self.assertTrue(mounts["/etc/skirnir-router/certs"])
        self.assertTrue(mounts["/etc/skirnir-router/tls"])
        self.assertIn("/var/lib/skirnir router", mounts, "octal escape decoded")
        self.assertTrue(mounts["/srv/readonly-fs"], "a read-only superblock makes the mount read-only")

    def test_longest_mount_wins(self):
        v = view(router_fs())
        self.assertTrue(v.read_only("/etc/hostname"))
        self.assertFalse(v.read_only("/etc/skirnir-router/copy/server.key"))
        self.assertTrue(v.read_only("/etc/skirnir-router/certs/privkey.pem"))
        self.assertTrue(v.read_only("/etc/skirnir-router-secrets"), "a sibling with a common prefix belongs to the root")


class ChangeableTest(unittest.TestCase):
    def test_router_unit(self):
        v = view(router_fs())
        self.assertFalse(v.changeable("/etc/skirnir-router/certs/fullchain.pem"), "read-only bind mount")
        self.assertFalse(v.changeable("/etc/skirnir-router/certs/privkey.pem"))
        self.assertFalse(v.changeable("/etc/skirnir-router/tls/server.crt"), "read-only bind mount (ADR-0082)")
        self.assertFalse(v.changeable("/etc/skirnir-router/tls/server.key"))
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"), "root owns it on a writable mount")
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.key"))
        self.assertFalse(v.changeable("/etc/skirnir-router/missing.pem"), "a missing file is not reported")

    def test_relative_path(self):
        # control_tls may name a relative path; it is opened from the working directory and judged there.
        v = view(router_fs(), cwd="/etc/skirnir-router")
        self.assertTrue(v.changeable("copy/server.crt"))
        self.assertFalse(v.changeable("certs/privkey.pem"))
        self.assertFalse(v.changeable("./tls/../tls/server.key"))

    def test_walk_ends_without_absolute_path(self):
        # The check runs before the router listens: the walk ends at "" instead of looping on dirname("") == "".
        fs = FakeFs({}, links={})
        fs.exists = lambda path: True
        fs.stat = fs.lstat = lambda path: st(uid=5, mode=0o755)
        done = []
        worker = threading.Thread(target=lambda: done.append(view(fs, euid=990, groups=(990,), cwd="")
                                                             .changeable("certs/key.pem")), daemon=True)
        worker.start()
        worker.join(5)
        self.assertEqual(done, [False], "the walk ends for a path that is not absolute")

    def test_folder_alone_is_enough(self):
        fs = router_fs(**{"/etc/skirnir-router/copy/server.crt": st(mode=0o444)})
        self.assertTrue(view(fs).changeable("/etc/skirnir-router/copy/server.crt"), "read-only file in a writable folder")

    def test_service_user(self):
        # A router with its own account: the folders and files belong to root and are only readable for it.
        fs = router_fs(**{"/etc/skirnir-router": st(mode=0o755), "/etc/skirnir-router/copy": st(mode=0o755)})
        v = view(fs, euid=990, groups=(990,))
        self.assertFalse(v.changeable("/etc/skirnir-router/copy/server.crt"))
        fs.entries["/etc/skirnir-router/copy/server.crt"] = st(uid=990)
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"), "it owns the file")
        fs.entries["/etc/skirnir-router/copy/server.crt"] = st(gid=990, mode=0o664)
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"), "its group may write")

    def test_owner_without_write_bit(self):
        # chmod needs ownership, not the write bit: on a writable mount the owner can grant itself write access.
        fs = router_fs(**{"/etc/skirnir-router": st(uid=990, mode=0o500), "/etc/skirnir-router/copy": st(mode=0o555),
                          "/etc/skirnir-router/copy/server.crt": st(uid=990, mode=0o400)})
        v = view(fs, euid=990, groups=(990,))
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"), "owned file with mode 0400")
        fs.entries["/etc/skirnir-router/copy/server.crt"] = st(mode=0o444)
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"), "owned folder above with mode 0500")
        fs.entries["/etc/skirnir-router"] = st(mode=0o755)
        self.assertFalse(v.changeable("/etc/skirnir-router/copy/server.crt"), "nothing owned on the way")

    def test_symlinked_folder_judged_by_its_target(self):
        # A link on a writable mount, in a folder the process may not change, pointing at a folder on a read-only
        # mount that the process owns: the owner cannot use its ownership there, so nothing is changeable.
        mounts = ("1 0 8:1 / / ro - ext4 /dev/sda1 rw\n"
                  "2 1 8:1 /srv/data /srv/data rw - ext4 /dev/sda1 rw\n"
                  "3 1 8:1 /srv/ro /srv/ro ro - ext4 /dev/sda1 rw\n")
        fs = FakeFs({"/": st(mode=0o755), "/srv": st(mode=0o755), "/srv/data": st(mode=0o755),
                     "/srv/ro": st(mode=0o755), "/srv/ro/dir": st(uid=990, mode=0o755),
                     "/srv/ro/dir/a.pem": st(mode=0o444)},
                    links={"/srv/data/link": "/srv/ro/dir"})
        v = view(fs, mounts, euid=990, groups=(990,))
        self.assertFalse(v.changeable("/srv/data/link/a.pem"))
        fs.entries["/srv/data"] = st(uid=990, mode=0o755)
        self.assertTrue(v.changeable("/srv/data/link/a.pem"), "the link itself can be swapped in an owned folder")

    def test_folder_above_can_be_swapped(self):
        # copy itself is root-owned and closed, but the account owns the folder that holds it and may rename copy.
        fs = router_fs(**{"/etc/skirnir-router": st(uid=990, mode=0o700), "/etc/skirnir-router/copy": st(mode=0o555)})
        v = view(fs, euid=990, groups=(990,))
        self.assertTrue(v.changeable("/etc/skirnir-router/copy/server.crt"))
        # ... unless that folder is a mount point (certs in the unit), which cannot be renamed
        fs.entries["/etc/skirnir-router/certs"] = st(mode=0o555)
        self.assertFalse(v.changeable("/etc/skirnir-router/certs/fullchain.pem"))

    def test_sticky_folder(self):
        mounts = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        fs = FakeFs({"/": st(mode=0o755), "/tmp": st(mode=0o1777), "/tmp/a.pem": st(uid=0), "/tmp/b.pem": st(uid=990)})
        v = view(fs, mounts, euid=990, groups=(990,))
        self.assertFalse(v.changeable("/tmp/a.pem"), "sticky: another owner's file cannot be replaced")
        self.assertTrue(v.changeable("/tmp/b.pem"))

    def test_symlink_target_and_link(self):
        fs = router_fs(**{"/etc/skirnir-router/certs/live": st(mode=0o755), "/etc/skirnir-router/certs/live/a.pem": st()})
        fs.links["/etc/skirnir-router/link.pem"] = "/etc/skirnir-router/certs/live/a.pem"
        self.assertTrue(view(fs).changeable("/etc/skirnir-router/link.pem"), "the link itself lies in a writable folder")
        fs.links = {"/etc/skirnir-router/certs/cur.pem": "/etc/skirnir-router/copy/server.crt"}
        self.assertTrue(view(fs).changeable("/etc/skirnir-router/certs/cur.pem"), "the target is writable")
        fs.links = {"/etc/skirnir-router/certs/gone.pem": "/etc/skirnir-router/copy/missing.pem"}
        self.assertFalse(view(fs).changeable("/etc/skirnir-router/certs/gone.pem"), "dangling link in a read-only folder")
        fs.links = {"/etc/skirnir-router/gone.pem": "/etc/skirnir-router/copy/missing.pem"}
        self.assertTrue(view(fs).changeable("/etc/skirnir-router/gone.pem"), "dangling link in a writable folder")

    def test_dac_override(self):
        mounts = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        fs = FakeFs({"/": st(mode=0o755), "/etc": st(mode=0o755), "/etc/x.pem": st(uid=5, mode=0o400)})
        self.assertFalse(view(fs, mounts, euid=990, groups=(990,)).changeable("/etc/x.pem"))
        self.assertTrue(view(fs, mounts, euid=990, groups=(990,), caps=1 << 1).changeable("/etc/x.pem"))


class PostureTest(unittest.TestCase):
    def cfg(self, cert, key):
        return SimpleNamespace(control_tls=(cert, key), client_auth={}, control_listen="127.0.0.1:8080", api_tls=True,
                               control_users={"a": "x"}, nodes={}, session_store=None)

    def codes(self, cfg, v):
        return [f for f in posture.findings(cfg, {}, v) if f.code == "tls_files_changeable"]

    def test_finding(self):
        v = view(router_fs())
        found = self.codes(self.cfg("/etc/skirnir-router/copy/server.crt", "/etc/skirnir-router/copy/server.key"), v)
        self.assertEqual([f.severity for f in found], ["medium", "medium"])
        self.assertIn("control_tls.key", found[1].text)
        for folder in ("/etc/skirnir-router/certs", "/etc/skirnir-router/tls"):
            self.assertIn(folder, found[0].next_step, "the next step names both protected folders")
        self.assertEqual(self.codes(self.cfg("/etc/skirnir-router/certs/fullchain.pem",
                                             "/etc/skirnir-router/certs/privkey.pem"), v), [])
        self.assertEqual(self.codes(self.cfg("/etc/skirnir-router/tls/server.crt",
                                             "/etc/skirnir-router/tls/server.key"), v), [])

    def test_without_view_no_finding(self):
        cfg = self.cfg("/etc/skirnir-router/copy/server.crt", "/etc/skirnir-router/copy/server.key")
        self.assertEqual(self.codes(cfg, None), [], "the CLI cannot see the unit's view")


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.isfile("/proc/self/mountinfo"), "Linux only")
class CurrentProcessTest(unittest.TestCase):
    def test_own_file_and_foreign_file(self):
        v = ProcessView.current()
        self.assertIsNotNone(v)
        with tempfile.NamedTemporaryFile() as f:
            self.assertTrue(v.changeable(f.name), "a file this process created")
        if os.geteuid() != 0 and os.path.isfile("/etc/hostname") and os.stat("/etc/hostname").st_uid == 0:
            self.assertFalse(v.changeable("/etc/hostname"), "root's file in root's folder")


@unittest.skipIf(sys.platform.startswith("linux"), "only where /proc/self does not exist")
class OtherPlatformTest(unittest.TestCase):
    def test_no_view(self):
        self.assertIsNone(ProcessView.current())


if __name__ == "__main__":
    unittest.main(verbosity=1)
