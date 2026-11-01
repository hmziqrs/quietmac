#!/usr/bin/env python3
"""End-to-end checks against real macOS machinery. Opt-in, not part of unittest.

    /usr/bin/python3 tests/integration.py

Everything happens in a throwaway temp folder:
  * an APFS container inside a disk image (never a real disk), used as the
    dev-volume container, with quietmac's sandbox (QUIETMAC_ROOT) and a private
    fstab; "admin" steps run as you, so nothing needs a password
  * a throwaway LaunchAgent (dev.quietmac.selftest, runs /bin/sleep) to test
    the agent-restart helper against real launchd; removed at the end
  * osascript without admin rights, to check root scripts survive AppleScript
The disk image is detached and the folder deleted even if a check fails.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
Q = str(ROOT / "bin" / "quietmac")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (%s)" % detail) if detail and not ok else ""), flush=True)


def sh(argv, env=None, stdin=subprocess.DEVNULL):
    p = subprocess.run(argv, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    return p.returncode, p.stdout


def main():
    tmp = Path(tempfile.mkdtemp(prefix="quietmac-it-"))
    image_dev = None
    try:
        # ---------------------------------------------------------- disk image
        img = tmp / "blank.dmg"
        rc, out = sh(["/usr/bin/hdiutil", "create", "-quiet", "-size", "300m", "-layout", "NONE", str(img)])
        rc, out = sh(["/usr/bin/hdiutil", "attach", "-nomount", str(img)])
        m = re.search(r"^(/dev/disk\d+)\s", out, re.M)  # skip hdiutil's deprecation warning
        assert m, out
        image_dev = m.group(1)
        rc, out = sh(["/usr/sbin/diskutil", "apfs", "createContainer", image_dev])
        container = re.findall(r"\b(disk\d+)\b", out.split("Disk from APFS operation:")[-1])[0]
        assert container != image_dev.replace("/dev/", ""), out

        sb = tmp / "root"
        (sb / "home").mkdir(parents=True)
        env = dict(os.environ, QUIETMAC_ROOT=str(sb), QUIETMAC_ADMIN_AS_USER="1", QUIETMAC_FSTAB=str(sb / "fstab"))
        mount = str(sb / "home" / "dev")
        rc, out = sh([Q, "devvol", "create", "--name", "QMIntegration", "--mount", mount, "--container", container,
                      "--yes"], env)
        st = json.loads((sb / "state" / "state.json").read_text())
        uuid = st["devvol"]["uuid"]
        check("devvol create: volume made, mounted, fstab line, no_log", os.path.ismount(mount)
              and ("UUID=%s " % uuid) in (sb / "fstab").read_text() and os.path.exists(mount + "/.fseventsd/no_log"), out)

        # -------------------------------------------- destroy safety (fix #2)
        sh(["/usr/sbin/diskutil", "unmount", mount])
        rc, out = sh([Q, "devvol", "destroy", "--yes"], env)
        check("destroy refuses while the volume is unmounted", rc == 1 and "isn't mounted" in out, out)
        sh(["/usr/sbin/diskutil", "mount", "nobrowse", "-mountPoint", mount, uuid])
        Path(mount, "keep-me.txt").write_text("data")
        rc, out = sh([Q, "devvol", "destroy", "--yes"], env)
        check("destroy refuses while files remain", rc == 1 and "still has files" in out, out)
        os.unlink(mount + "/keep-me.txt")

        # ------------------------------- migration: failed switch (fix #3)
        proj = sb / "home" / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "a.txt").write_text("hello")
        os.chmod(str(sb / "home"), 0o555)  # renaming the original aside will now fail
        try:
            rc, out = sh([Q, "migrate", "run", str(proj), "--yes"], env)
        finally:
            os.chmod(str(sb / "home"), 0o755)
        st = json.loads((sb / "state" / "state.json").read_text())
        rec = st["migrations"].get(str(proj), {})
        check("failed switch puts things back (original intact, copy unswitched)",
              rc == 1 and "couldn't move the original aside" in out and proj.is_dir() and not proj.is_symlink()
              and not os.path.exists(mount + "/proj") and os.path.isdir(mount + "/proj.quietmac-partial")
              and rec.get("status") == "failed", out)
        rc, out = sh([Q, "migrate", "rollback", str(proj), "--yes"], env)
        check("rollback of the failed run removes the partial copy",
              rc == 0 and not os.path.exists(mount + "/proj.quietmac-partial"), out)

        # ------------------------- migration: resume without --yes (fix #10)
        rc, out = sh([Q, "migrate", "run", str(proj), "--yes"], env)
        check("migration completes", rc == 0 and proj.is_symlink(), out)
        os.unlink(str(proj))  # simulate an interruption right before the symlink
        st = json.loads((sb / "state" / "state.json").read_text())
        st["migrations"][str(proj)]["status"] = "switched"
        (sb / "state" / "state.json").write_text(json.dumps(st))
        rc, out = sh([Q, "migrate", "run", str(proj)], env)  # no --yes, no terminal
        check("interrupted migration resumes without --yes", rc == 0 and proj.is_symlink()
              and os.readlink(str(proj)) == mount + "/proj", out)
        rc, out = sh([Q, "migrate", "status", "--json"], env)
        try:
            json.loads(out)
            check("migrate status --json is valid JSON (fix #11)", rc == 0)
        except ValueError:
            check("migrate status --json is valid JSON (fix #11)", False, out[:200])

        # ----------------------------------------------- tidy up the volume
        rc, out = sh([Q, "migrate", "rollback", str(proj), "--yes"], env)
        rc2, out2 = sh([Q, "migrate", "cleanup", "--yes"], env)
        rc3, out3 = sh([Q, "devvol", "destroy", "--yes"], env)
        check("rollback, cleanup and destroy leave nothing behind", rc == 0 and rc2 == 0 and rc3 == 0
              and proj.is_dir() and not proj.is_symlink() and (proj / "src" / "a.txt").read_text() == "hello"
              and ("UUID=%s " % uuid) not in (sb / "fstab").read_text(), out + out2 + out3)

        # ---------------------------------- modules and CLI (fixes #8, #9, #12)
        section(cli_checks, tmp)
        section(launchd_checks, tmp)
        section(sudoers_checks, tmp)
    finally:
        if image_dev:
            sh(["/usr/bin/hdiutil", "detach", "-force", image_dev])
        subprocess.run(["/bin/chmod", "-R", "u+w", str(tmp)])
        subprocess.run(["/bin/rm", "-rf", str(tmp)])
    failed = [n for n, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    return 1 if failed else 0


def section(fn, tmp):
    try:
        fn(tmp)
    except Exception as e:
        check(fn.__name__ + " ran", False, "%s: %s" % (type(e).__name__, e))


def cli_checks(tmp):
    if True:
        sb2 = tmp / "root2"
        env2 = dict(os.environ, QUIETMAC_ROOT=str(sb2))
        rc, out = sh([Q, "apply", "spotlight-off", "reap-appstore", "--yes"], env2)  # the failing one first
        st = json.loads((sb2 / "state" / "state.json").read_text())
        check("a failing module doesn't block the others; exit code 1",
              rc == 1 and "reap-appstore" in st["modules"] and "spotlight-off" not in st["modules"], out)
        rc, out = sh([Q, "reaper", "resume"], env2)
        check("reaper resume when it's running says so", rc == 0 and "already running" in out, out)
        (sb2 / "LaunchAgents" / "dev.quietmac.reaper.plist").unlink()  # now only partly applied
        rc, out = sh([Q, "reaper", "resume"], env2)
        check("reaper resume when it isn't installed says so", rc == 1 and "isn't installed" in out, out)
        rc, out = sh([Q, "revert", "reap-appstore", "--yes"], env2)
        st = json.loads((sb2 / "state" / "state.json").read_text())
        check("a partly applied module can be reverted", rc == 0 and "reap-appstore" not in st["modules"]
              and not st["reaper"]["groups"], out)



def launchd_checks(tmp):
    if sh(["/bin/launchctl", "print", "gui/%d" % os.getuid()])[0] != 0:
        print("SKIP  restarting an already-loaded LaunchAgent works  (no GUI login session here)")
        return
    if True:
        label = "dev.quietmac.selftest"
        plist = tmp / (label + ".plist")
        import plistlib
        with open(plist, "wb") as fh:
            plistlib.dump({"Label": label, "ProgramArguments": ["/bin/sleep", "600"], "RunAtLoad": True}, fh)
        code = ("import sys; sys.path.insert(0, %r); from pathlib import Path; from quietmac import core; "
                "core.bootstrap_agent(%r, Path(%r))" % (str(ROOT), label, str(plist)))
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("QUIETMAC_")}
        target = "gui/%d/%s" % (os.getuid(), label)
        pid_of = lambda: (re.search(r"^\s*pid = (\d+)", sh(["/bin/launchctl", "print", target])[1], re.M) or [None, None])[1]
        try:
            r1 = sh([sys.executable, "-c", code], clean_env)
            time.sleep(0.5)
            p1 = pid_of()
            r2 = sh([sys.executable, "-c", code], clean_env)  # already loaded: must restart, not fail
            time.sleep(0.5)
            p2 = pid_of()
            check("restarting an already-loaded LaunchAgent works", r1[0] == 0 and r2[0] == 0 and p1 and p2 and p1 != p2,
                  "%s %s pids %s %s" % (r1, r2, p1, p2))
        finally:
            sh(["/bin/launchctl", "bootout", target])



def sudoers_checks(tmp):
    if True:
        sys.path.insert(0, str(ROOT))
        from quietmac import core
        from quietmac.modules import warp
        text = warp.sudoers_text()
        lines = warp.sudoers_script(text).split("\n")
        cut = next(i for i, l in enumerate(lines) if l.startswith("if ! /usr/sbin/visudo -cf"))
        out_file = tmp / "sudoers.out"
        safe = "\n".join(lines[:cut + 1] + ['cp "$T" %s' % core.sh_quote(str(out_file)), '/bin/rm -f "$T"'])
        rc, out = sh(["/usr/bin/osascript", "-e", "do shell script %s without altering line endings"
                      % core._applescript_string(safe)])
        check("sudoers rule survives AppleScript escaping and validates", rc == 0 and out_file.read_text() == text, out)
        check("the root script never reads a user-writable file",
              str(Path.home()) not in warp.sudoers_script(text) and "mktemp" in warp.sudoers_script(text))


if __name__ == "__main__":
    sys.exit(main())
