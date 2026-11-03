"""The dev volume: an APFS volume for code and build caches with fseventsd's
persistent event log and Spotlight indexing turned off.

Why: with many coding agents and builds running in parallel, every
`node_modules`/`target`/`build` churn goes through fseventsd, which keeps a
persistent per-volume event history, and through Spotlight. On a separate
volume both can be switched off for code only:

  * `.fseventsd/no_log` at the volume root: fseventsd keeps no history for it.
    Live file watching (fs.watch, watchman, editors, dev servers) still works;
    tested on macOS 27, events arrived the same as on a normal volume.
  * Spotlight indexing off for that volume only (mdutil -i off), so Spotlight
    keeps working everywhere else.
  * The volume shares space with the rest of the disk (APFS, no fixed size).
  * Mounted by /etc/fstab (by UUID) at a folder in your home, e.g. ~/dev, with
    `nobrowse` (no Desktop/Finder sidebar icon) and `owners` (real
    permissions). While unmounted, the mount folder is root-owned with mode 000,
    so nothing can be written to the wrong disk by accident.

Unencrypted on purpose (no unlock step at boot). Your main volume keeps
FileVault; only what you put on this volume is outside it. On Apple silicon
the SSD still encrypts everything in hardware.

Prior art: elithrar/dotfiles repos-volume.sh (same approach, single ~/repos).
"""

from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core
from .core import Step

FSTAB = Path(os.environ["QUIETMAC_FSTAB"]) if core.SANDBOX and os.environ.get("QUIETMAC_FSTAB") else Path("/etc/fstab")
MARK = "# quietmac dev volume"
SYSTEM_ENTRIES = {".fseventsd", ".Spotlight-V100", ".metadata_never_index", ".Trashes", ".TemporaryItems",
                  ".DS_Store", ".DocumentRevisions-V100", ".VolumeIcon.icns", ".quietmac-devvol"}
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def diskutil_info(target: str) -> Dict[str, Any]:
    r = core.run(["/usr/sbin/diskutil", "info", "-plist", target])
    if not r.ok:
        return {}
    try:
        return plistlib.loads(r.out.encode())
    except Exception:
        return {}


def data_container() -> str:
    c = diskutil_info("/System/Volumes/Data").get("APFSContainerReference")
    if not c:
        raise core.QuietmacError("couldn't find the APFS container of the startup disk")
    return c


def container_free(container: str) -> Optional[int]:
    return diskutil_info(container).get("APFSContainerFree") or diskutil_info("/System/Volumes/Data").get("APFSContainerFree")


def is_mounted_at(mount: str, uuid: Optional[str] = None) -> bool:
    if not os.path.ismount(mount):
        return False
    if uuid:
        return diskutil_info(mount).get("VolumeUUID") == uuid
    return True


def fstab_lines() -> List[str]:
    try:
        return FSTAB.read_text().splitlines()
    except OSError:
        return []


def fstab_entry(uuid: str, mount: str) -> str:
    return "UUID=%s %s apfs rw,nobrowse,owners" % (uuid, mount)


def status(st: Dict[str, Any]) -> Dict[str, Any]:
    dv = st.get("devvol")
    if not dv:
        return {"exists": False}
    mount, uuid = dv["mount"], dv["uuid"]
    info = diskutil_info(uuid)
    mounted = is_mounted_at(mount, uuid)
    res = {
        "exists": bool(info), "name": dv["name"], "mount": mount, "uuid": uuid,
        "device": info.get("DeviceIdentifier"), "mounted": mounted,
        "fstab": any(l.startswith("UUID=%s " % uuid) for l in fstab_lines()),
        "no_log": mounted and os.path.exists(os.path.join(mount, ".fseventsd", "no_log")),
        "spotlight_off": None, "used_bytes": None,
    }
    if mounted:
        s = core.out(["/usr/bin/mdutil", "-s", mount])
        res["spotlight_off"] = True if "disabled" in s else (False if "enabled" in s else None)
        try:
            v = os.statvfs(mount)
            res["free_bytes"] = v.f_bavail * v.f_frsize
        except OSError:
            pass
        res["used_bytes"] = info.get("CapacityInUse") or info.get("APFSVolumeUsed")  # name varies by release
    return res


def validate(name: str, mount: str) -> str:
    mount = os.path.abspath(os.path.expanduser(mount))
    if not SAFE_NAME.match(name):
        raise core.QuietmacError("volume name: use 1-32 letters, digits, '-' or '_'")
    if not SAFE_PATH.match(mount):
        raise core.QuietmacError("mount path: use letters, digits, '.', '_', '-' and '/' only (fstab can't take spaces)")
    if not core.SANDBOX and not mount.startswith(str(core.HOME) + "/"):
        raise core.QuietmacError("mount the dev volume inside your home folder, e.g. ~/dev")
    if os.path.islink(mount):
        raise core.QuietmacError("%s is a symlink" % mount)
    if os.path.ismount(mount):
        raise core.QuietmacError("something is already mounted at %s" % mount)
    if os.path.exists(mount):
        if not os.path.isdir(mount):
            raise core.QuietmacError("%s exists and isn't a folder" % mount)
        if os.listdir(mount):
            raise core.QuietmacError("%s isn't empty. Pick a new folder, then move projects in with `quietmac migrate`." % mount)
    if any(MARK in l for l in fstab_lines()):
        raise core.QuietmacError("/etc/fstab already has a quietmac dev volume entry (see `quietmac devvol status`)")
    vols = core.out(["/usr/sbin/diskutil", "list"])
    if re.search(r"\bAPFS Volume %s\b" % re.escape(name), vols):
        raise core.QuietmacError("an APFS volume called %s already exists; pick another name" % name)
    return mount


def create_script(name: str, mount: str, container: str, sandbox: bool = False) -> str:
    """Root script. One command per line, so `set -e` stops at the first failure
    (it ignores failures inside `a && b`). Prints QUIETMAC_UUID early, so a
    half-finished run can still be cleaned up with `quietmac devvol destroy`."""
    q = core.sh_quote
    owners = "" if sandbox else "-mountOptions owners "
    lines = [
        "set -eu",
        "NAME=%s MP=%s CONT=%s FSTAB=%s" % (q(name), q(mount), q(container), q(str(FSTAB))),
        "# fstab first: macOS 27's system policy denies creating /etc/fstab, even for root, and then nothing",
        "# should have been created yet",
        'if [ -f "$FSTAB" ]; then /bin/cp -p "$FSTAB" "$FSTAB.quietmac-backup"; else /usr/bin/touch "$FSTAB"; fi',
        'OUT=$(/usr/sbin/diskutil apfs addVolume "$CONT" APFS "$NAME" -nomount 2>&1) || { echo "$OUT" >&2; exit 1; }',
        "DEV=$(printf '%s\\n' \"$OUT\" | /usr/bin/sed -n 's/.*\\(disk[0-9][0-9]*s[0-9][0-9]*\\).*/\\1/p' | /usr/bin/tail -1)",
        '[ -n "$DEV" ] || { echo "could not find the new volume in: $OUT" >&2; exit 1; }',
        'UUID=$(/usr/sbin/diskutil info -plist "$DEV" | /usr/bin/plutil -extract VolumeUUID raw -o - -)',
        'echo "QUIETMAC_DEV=$DEV"; echo "QUIETMAC_DEV=$DEV" >&2',
        'echo "QUIETMAC_UUID=$UUID"; echo "QUIETMAC_UUID=$UUID" >&2',
        '/bin/mkdir -p "$MP"',
    ]
    if not sandbox:
        lines += ['/usr/sbin/chown root:wheel "$MP"  # while unmounted, writes here fail loudly', '/bin/chmod 000 "$MP"']
    lines += [
        "printf '%%s\\nUUID=%%s %%s apfs rw,nobrowse,owners\\n' %s \"$UUID\" \"$MP\" >> \"$FSTAB\"" % q(MARK),
        'echo QUIETMAC_FSTAB_OK',
    ]
    if not sandbox:
        lines += ['/usr/sbin/diskutil enableOwnership "$DEV" >/dev/null || true  # fstab and the mount also say owners']
    lines += [
        '/usr/sbin/diskutil mount nobrowse %s-mountPoint "$MP" "$DEV" >/dev/null' % owners,
    ]
    if not sandbox:
        lines += ['/usr/sbin/chown %s "$MP"' % q("%s:staff" % core.USER)]
    lines += [
        '/bin/chmod 755 "$MP"',
        '/bin/mkdir -p "$MP/.fseventsd"',
        '/usr/bin/touch "$MP/.fseventsd/no_log"',
        '/usr/bin/touch "$MP/.metadata_never_index"',
    ]
    if not sandbox:
        lines += ['/usr/sbin/chown %s "$MP/.metadata_never_index"' % q("%s:staff" % core.USER)]
    lines += [
        '/usr/bin/mdutil -i off "$MP" >/dev/null 2>&1 || echo "note: mdutil could not turn indexing off" >&2',
        "# remount so fseventsd sees no_log from the start",
        '/usr/sbin/diskutil unmount "$MP" >/dev/null',
        '/usr/sbin/diskutil mount nobrowse %s-mountPoint "$MP" "$DEV" >/dev/null' % owners,
        "echo QUIETMAC_OK",
    ]
    return "\n".join(lines) + "\n"


def create_steps(st: Dict[str, Any], name: str = "Dev", mount: str = "~/dev", container: Optional[str] = None) -> List[Step]:
    if st.get("devvol"):
        raise core.QuietmacError("a dev volume already exists: %s at %s" % (st["devvol"]["name"], st["devvol"]["mount"]))
    mount = validate(name, mount)
    container = container or data_container()
    free = container_free(container)
    if free is not None and free < 20 * 1024 ** 3:
        raise core.QuietmacError("less than 20 GB free on the disk (%s); free some space first" % core.human_bytes(free))
    script = create_script(name, mount, container, sandbox=core.SANDBOX)

    def create() -> None:
        try:
            outp = core.admin(script, "create the %s dev volume at %s" % (name, mount))
            failure = None
        except core.AdminError as e:
            outp, failure = e.out, e
        dev = re.search(r"QUIETMAC_DEV=(\S+)", outp)
        uuid = re.search(r"QUIETMAC_UUID=(\S+)", outp)
        if uuid:
            # record even a half-finished volume, so `quietmac devvol destroy` can remove it
            st["devvol"] = {"name": name, "mount": mount, "uuid": uuid.group(1), "container": container,
                            "created_at": core.now_iso(), "complete": "QUIETMAC_OK" in outp}
            core.save_state(st)
        if failure or "QUIETMAC_OK" not in outp:
            hint = (" The volume was created (UUID %s) but setup didn't finish: `quietmac devvol destroy` removes it "
                    "and its /etc/fstab line." % uuid.group(1)) if uuid else " Nothing was created."
            raise core.StepError("dev volume setup failed: %s.%s" % (failure or "no completion marker", hint))
        core.log_action("created dev volume %s (%s, UUID %s) at %s" % (name, dev.group(1), uuid.group(1), mount))

    return [Step("create APFS volume '%s' in %s, mount it at %s via /etc/fstab, turn off fseventsd logging and "
                 "Spotlight on it %s" % (name, container, mount, core.yellow("[root]")), fn=create,
                 show="\n".join("      # " + l for l in script.strip().splitlines()))]


def mount_steps(st: Dict[str, Any]) -> List[Step]:
    dv = st.get("devvol")
    if not dv:
        raise core.QuietmacError("no dev volume")
    if is_mounted_at(dv["mount"], dv["uuid"]):
        return []
    script = "/usr/sbin/diskutil mount nobrowse -mountOptions owners -mountPoint %s %s" % (
        core.sh_quote(dv["mount"]), core.sh_quote(dv["uuid"]))
    return [Step("mount the dev volume at %s" % dv["mount"], script=script)]


def user_entries(mount: str) -> List[str]:
    try:
        return sorted(e for e in os.listdir(mount) if e not in SYSTEM_ENTRIES)
    except OSError:
        return []


def destroy_steps(st: Dict[str, Any], force: bool = False) -> List[Step]:
    dv = st.get("devvol")
    if not dv:
        raise core.QuietmacError("no dev volume recorded")
    active = [src for src, m in st["migrations"].items() if m.get("status") == "done"]
    if active:
        raise core.QuietmacError("these folders still live on the dev volume; roll them back first "
                                 "(quietmac migrate rollback ...): " + ", ".join(active))
    moved = [c for c, m in st["caches"].items() if m.get("status") == "moved"]
    if moved:
        raise core.QuietmacError("restore these caches first (quietmac cache restore ...): " + ", ".join(moved))
    mount, uuid = dv["mount"], dv["uuid"]
    if is_mounted_at(mount, uuid):
        left = user_entries(mount)
        if left and not force:
            raise core.QuietmacError("the dev volume still has files (%s). Move them off, or pass --force to delete "
                                     "them with the volume." % ", ".join(left[:8]))
    elif diskutil_info(uuid) and not force:
        raise core.QuietmacError("the dev volume isn't mounted, so I can't check it's empty. Mount it first "
                                 "(quietmac devvol mount), or pass --force to delete it unchecked.")
    script = """set -eu
MP=%(mp)s UUID=%(uuid)s FSTAB=%(fstab)s
if /sbin/mount | /usr/bin/grep -q " on $MP "; then /usr/sbin/diskutil unmount "$MP" >/dev/null; fi
/usr/sbin/diskutil apfs deleteVolume "$UUID" >/dev/null
if [ -f "$FSTAB" ]; then
  /bin/cp -p "$FSTAB" "$FSTAB.quietmac-backup"
  /usr/bin/grep -v -e "^UUID=$UUID " -e %(mark)s "$FSTAB.quietmac-backup" > "$FSTAB" || true
fi
/bin/rmdir "$MP" 2>/dev/null || true
""" % {"mp": core.sh_quote(mount), "uuid": core.sh_quote(uuid), "fstab": core.sh_quote(str(FSTAB)),
       "mark": core.sh_quote("^" + MARK)}

    def forget() -> None:
        st.pop("devvol", None)
        core.save_state(st)
        core.log_action("deleted dev volume %s (UUID %s)" % (dv["name"], uuid))

    return [Step("unmount and delete the dev volume, and remove its /etc/fstab line", script=script),
            Step("forget the dev volume in quietmac state", fn=forget)]
