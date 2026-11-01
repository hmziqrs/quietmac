#!/usr/bin/env python3
"""quietmac daemon reaper: keep unwanted per-user Apple daemons dead.

Why this exists: on macOS 26/27 with SIP on, Apple's own per-user agents
(appstoreagent, mediaanalysisd, the Siri helpers, ...) are "system service |
tle system" launchd jobs. `launchctl bootout` is refused (error 150) and
`launchctl disable` is ignored: launchd relaunches them on demand over XPC or
mach. So we poll and SIGKILL matching processes owned by this user. They are
jetsam-managed ("supports pressured exit"), so SIGKILL is the same thing the
kernel does to them under memory pressure.

Cost: the process list comes straight from libproc (proc_listallpids +
proc_pidinfo), ~1 ms of CPU per scan. Spawning `ps` instead costs ~45 ms per
scan, which at a 5 s poll is ~1% of a core around the clock.

Safety:
  * only processes owned by this user (never root daemons)
  * a hard-coded never-kill list, applied even if the config names them
  * churn guard, in two stages. A name killed CHURN_MAX times within
    CHURN_WINDOW seconds has a client that keeps relaunching it, so each kill
    buys a cold start. It then moves to WATCH mode: it may live while idle and
    is killed only after using more than WATCH_CPU% of a core for WATCH_GRACE
    seconds. (Measured: after giving up on mediaanalysisd outright, it and the
    root ANECompilerService it drives used 4.6 min of CPU each over 4.5 h.)
    If it still churns in watch mode, it's left alone until next login.

Config: REAPER_CONF (default ~/.config/quietmac/reaper.conf), one executable
basename per line, '#' comments. Re-read whenever it changes; no restart needed.

Knobs (environment): REAPER_CONF, REAPER_POLL (5), REAPER_CHURN_MAX (10),
REAPER_CHURN_WINDOW (600), REAPER_WATCH_CPU (10, percent of one core),
REAPER_WATCH_GRACE (20 s), REAPER_LOG (~/Library/Logs/quietmac/reaper.log),
REAPER_DRY_RUN (1 = log only), REAPER_ONESHOT (1 = one scan, then exit).
"""

import ctypes
import os
import signal
import subprocess
import sys
import time
import traceback

# Killing any of these breaks the session or starts a relaunch loop. Measured on
# macOS 27: chronod is relaunched every ~5 s; the "Siri AI" app (launchd label
# com.apple.campo) is relaunched every ~10 s and drags siriinferenced and
# mediaanalysisd back with it. Keep in sync with quietmac/reaper.py.
NEVER_KILL = frozenset("""
launchd loginwindow WindowServer Finder Dock SystemUIServer ControlCenter
NotificationCenter WindowManager chronod fseventsd cfprefsd distnoted
usernoted UserEventAgent lsd trustd secd securityd tccd sharedfilelistd
siriinferenced talagentd coreservicesd useractivityd
StartupDiskWidgetExtension BluetoothSettingsAppIntentsWidgetExtension
Python python3 python3.9 bash zsh sh login sudo
""".split()) | frozenset(["Siri AI"])


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


CONF = os.path.expanduser(os.environ.get("REAPER_CONF", "~/.config/quietmac/reaper.conf"))
POLL = max(0.2, env_float("REAPER_POLL", 5.0))
CHURN_MAX = int(env_float("REAPER_CHURN_MAX", 10))
CHURN_WINDOW = env_float("REAPER_CHURN_WINDOW", 600.0)
WATCH_CPU = env_float("REAPER_WATCH_CPU", 10.0) / 100.0
WATCH_GRACE = env_float("REAPER_WATCH_GRACE", 20.0)
LOG_PATH = os.path.expanduser(os.environ.get("REAPER_LOG", "~/Library/Logs/quietmac/reaper.log"))
DRY_RUN = os.environ.get("REAPER_DRY_RUN", "").lower() not in ("", "0", "false", "no")
ONESHOT = os.environ.get("REAPER_ONESHOT", "") == "1"
SUMMARY_EVERY = 3600.0
LOG_MAX = 1 << 20
UID = os.getuid()


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        d = os.path.dirname(LOG_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a") as fh:
            fh.write(line)
    except OSError:
        pass


def read_names(path):
    names = set()
    try:
        with open(path) as fh:
            for raw in fh:
                n = raw.split("#", 1)[0].strip()
                if n:
                    names.add(n)
    except OSError:
        pass
    return frozenset(names - NEVER_KILL)


# --------------------------------------------------------------------------
# process listing


class _ShortBSDInfo(ctypes.Structure):
    # struct proc_bsdshortinfo (sys/proc_info.h), PROC_PIDT_SHORTBSDINFO = 13
    _fields_ = [
        ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("status", ctypes.c_uint32), ("comm", ctypes.c_char * 16), ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32),
        ("rfu", ctypes.c_uint32),
    ]


class _TaskInfo(ctypes.Structure):
    # struct proc_taskinfo, PROC_PIDTASKINFO = 4. Times are mach ticks.
    _fields_ = ([(n, ctypes.c_uint64) for n in ("virtual_size", "resident_size", "total_user", "total_system",
                                                "threads_user", "threads_system")]
                + [(n, ctypes.c_int32) for n in ("policy", "faults", "pageins", "cow_faults", "messages_sent",
                                                 "messages_received", "syscalls_mach", "syscalls_unix", "csw",
                                                 "threadnum", "numrunning", "priority")])


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class LibprocLister(object):
    method = "libproc"

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listallpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.lib.proc_pidpath.restype = ctypes.c_int
        self.info = _ShortBSDInfo()
        self.size = ctypes.sizeof(self.info)
        self.task = _TaskInfo()
        tb = _Timebase()
        ctypes.CDLL("/usr/lib/libSystem.B.dylib").mach_timebase_info(ctypes.byref(tb))
        self.tick_seconds = float(tb.numer) / float(tb.denom) / 1e9 if tb.denom else 1e-9
        self.path = ctypes.create_string_buffer(4096)
        self.cap = 0
        self.buf = None
        if self.size != 64:
            raise OSError("unexpected proc_bsdshortinfo size %d" % self.size)
        if ctypes.sizeof(self.task) != 96:
            raise OSError("unexpected proc_taskinfo size %d" % ctypes.sizeof(self.task))
        self.processes(frozenset())  # fail fast if the API is unusable

    def cpu_seconds(self, pid):
        """Total CPU time of pid, or None if it's gone."""
        if self.lib.proc_pidinfo(pid, 4, 0, ctypes.byref(self.task), 96) != 96:
            return None
        return (self.task.total_user + self.task.total_system) * self.tick_seconds

    def processes(self, names):
        """[(pid, name)] of this user's processes whose basename is in names."""
        n = self.lib.proc_listallpids(None, 0)
        if n <= 0:
            raise OSError("proc_listallpids failed")
        if n + 64 > self.cap:
            self.cap = n + 256
            self.buf = (ctypes.c_int * self.cap)()
        n = self.lib.proc_listallpids(self.buf, ctypes.sizeof(self.buf))
        # The kernel keeps the executable's file name (p_comm) cut to 15
        # bytes. Shorter names match it exactly; names that may have been cut
        # are confirmed with the full executable path.
        keys = set(name.encode("utf-8")[:15] for name in names)
        found = []
        for i in range(max(0, n)):
            pid = self.buf[i]
            if pid <= 0:
                continue
            if self.lib.proc_pidinfo(pid, 13, 0, ctypes.byref(self.info), self.size) != self.size:
                continue
            if self.info.uid != UID:
                continue
            comm = self.info.comm
            if comm not in keys:
                continue
            if len(comm) < 15:
                name = comm.decode("utf-8", "replace")
                if name in names:
                    found.append((pid, name))
                continue
            if self.lib.proc_pidpath(pid, self.path, 4096) <= 0:
                continue
            name = os.path.basename(self.path.value.decode("utf-8", "replace"))
            if name in names:
                found.append((pid, name))
        return found


class PsLister(object):
    method = "ps"

    def cpu_seconds(self, pid):
        try:
            out = subprocess.run(["/bin/ps", "-o", "time=", "-p", str(pid)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
        except OSError:
            return None
        if not out:
            return None
        total = 0.0
        for part in out.replace("-", ":").split(":"):
            total = total * 60 + float(part)
        return total

    def processes(self, names):
        try:
            res = subprocess.run(["/bin/ps", "-axo", "pid=,uid=,comm="], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True).stdout
        except OSError:
            return []
        found = []
        for line in res.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            name = os.path.basename(parts[2])
            if int(parts[1]) == UID and name in names:
                found.append((int(parts[0]), name))
        return found


def make_lister():
    try:
        return LibprocLister()
    except (OSError, AttributeError) as e:
        log("libproc unavailable (%s); falling back to ps" % e)
        return PsLister()


# --------------------------------------------------------------------------


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    lister = make_lister()
    names = read_names(CONF)
    try:
        conf_mtime = os.stat(CONF).st_mtime
    except OSError:
        conf_mtime = None
    log("reaper started pid=%d names=%s poll=%.1fs method=%s%s" % (
        os.getpid(), ",".join(sorted(names)) or "(none)", POLL, lister.method, " DRY-RUN" if DRY_RUN else ""))
    seen = set()  # names already logged individually
    counts = {}  # name -> kills since last summary
    recent = {}  # name -> kill times within CHURN_WINDOW
    watch = set()  # names in watch mode (killed only when busy)
    given_up = set()
    samples = {}  # pid -> (cpu seconds, wall time, busy since or None)
    last_summary = time.time()
    while True:
        try:
            m = os.stat(CONF).st_mtime
        except OSError:
            m = None
        if m != conf_mtime:
            conf_mtime = m
            new = read_names(CONF)
            if new != names:
                names = new
                given_up &= names
                watch &= names
                log("config reloaded: names=%s" % (",".join(sorted(names)) or "(none)"))
        active = names - given_up
        alive = set()
        for pid, name in (lister.processes(active) if active else []):
            now = time.time()
            if name in watch:
                alive.add(pid)
                cpu = lister.cpu_seconds(pid)
                prev = samples.get(pid)
                if cpu is None:
                    continue
                busy_since = None
                usage = 0.0
                if prev is not None and now > prev[1]:
                    usage = (cpu - prev[0]) / (now - prev[1])
                    if usage >= WATCH_CPU:
                        busy_since = prev[2] if prev[2] is not None else prev[1]
                samples[pid] = (cpu, now, busy_since)
                if busy_since is None or now - busy_since < WATCH_GRACE:
                    continue
                reason = "busy: %.0f%% of a core for %.0fs" % (usage * 100, now - busy_since)
            else:
                reason = ""
            if DRY_RUN:
                log("DRY-RUN would kill pid=%d %s %s" % (pid, name, reason))
                continue
            times = [t for t in recent.get(name, ()) if now - t < CHURN_WINDOW]
            if len(times) >= CHURN_MAX:
                recent[name] = []
                if name in watch:
                    watch.discard(name)
                    given_up.add(name)
                    log("GAVE UP on %s until next login: it keeps relaunching even when killed only while busy "
                        "(remove it: quietmac reaper remove %s)" % (name, name))
                    continue
                watch.add(name)
                log("CHURN on %s: killed %d times in %.0fm, something keeps relaunching it. Watch mode: it may "
                    "run while idle and is killed after %.0fs above %.0f%% of a core" % (
                        name, len(times), CHURN_WINDOW / 60, WATCH_GRACE, WATCH_CPU * 100))
                alive.add(pid)
                cpu = lister.cpu_seconds(pid)
                if cpu is not None:
                    samples[pid] = (cpu, now, None)
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
            samples.pop(pid, None)
            times.append(now)
            recent[name] = times
            if name not in seen or reason:
                seen.add(name)
                log("KILLED pid=%d %s%s" % (pid, name, (" (%s)" % reason) if reason else " (repeats summarized hourly)"))
            else:
                counts[name] = counts.get(name, 0) + 1
        for pid in list(samples):
            if pid not in alive:
                samples.pop(pid, None)
        now = time.time()
        if counts and now - last_summary >= SUMMARY_EVERY:
            log("relaunch kills in last %.0fm: %s" % (
                (now - last_summary) / 60, ", ".join("%s=%d" % kv for kv in sorted(counts.items()))))
            counts.clear()
            last_summary = now
        if ONESHOT:
            return
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        log("reaper crashed:\n" + traceback.format_exc())
        sys.exit(1)
