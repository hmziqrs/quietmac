"""Shared plumbing: paths, logging, running commands, admin rights, state, UI.

Everything quietmac changes is recorded in two places:
  * STATE_FILE (JSON): what is applied and the previous values needed to undo it.
  * ACTIONS_LOG (text): a timestamped ledger of every change, including the
    exact root scripts that ran.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import pwd
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

UID = os.getuid()
USER = pwd.getpwuid(UID).pw_name
HOME = Path(pwd.getpwuid(UID).pw_dir)

# QUIETMAC_ROOT moves every file quietmac owns under one directory and turns
# off launchctl/admin side effects. Used by the tests; never set it for real.
_ROOT = os.environ.get("QUIETMAC_ROOT")
SANDBOX = bool(_ROOT)


def _p(default: Path, sub: str) -> Path:
    return Path(_ROOT) / sub if _ROOT else default


STATE_DIR = _p(HOME / ".local/state/quietmac", "state")
CONFIG_DIR = _p(HOME / ".config/quietmac", "config")
SHARE_DIR = _p(HOME / ".local/share/quietmac", "share")
LOG_DIR = _p(HOME / "Library/Logs/quietmac", "logs")
AGENTS_DIR = _p(HOME / "Library/LaunchAgents", "LaunchAgents")
BIN_DIR = _p(HOME / ".local/bin", "bin")
CLAUDE_PROJECTS = _p(HOME / ".claude/projects", "claude-projects")
STATE_FILE = STATE_DIR / "state.json"
ACTIONS_LOG = LOG_DIR / "actions.log"
LABEL_PREFIX = "dev.quietmac"
REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_DIR = REPO_ROOT / "payload"

# --------------------------------------------------------------------------
# output

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return "\033[%sm%s\033[0m" % (code, s) if _TTY else s


def bold(s: str) -> str:
    return _c("1", s)


def dim(s: str) -> str:
    return _c("2", s)


def green(s: str) -> str:
    return _c("32", s)


def yellow(s: str) -> str:
    return _c("33", s)


def red(s: str) -> str:
    return _c("31", s)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(yellow("warning: ") + msg, file=sys.stderr, flush=True)


class QuietmacError(Exception):
    """A user-facing failure: print the message, exit non-zero."""


# --------------------------------------------------------------------------
# ledger


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def log_action(msg: str) -> None:
    """Append to the human-readable ledger of every change quietmac makes."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(ACTIONS_LOG, "a") as fh:
            for i, line in enumerate(msg.rstrip("\n").split("\n")):
                fh.write("%s %s\n" % (now_iso() if i == 0 else " " * 19, line))
    except OSError:
        pass


# --------------------------------------------------------------------------
# running commands


class Result:
    __slots__ = ("rc", "out", "err")

    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out, err

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def __repr__(self) -> str:
        return "Result(rc=%d, out=%r, err=%r)" % (self.rc, self.out[:80], self.err[:80])


def run(argv: Sequence[str], input: Optional[str] = None, timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> Result:
    try:
        p = subprocess.run(list(argv), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout, env=env, cwd=cwd)
    except FileNotFoundError as e:
        return Result(127, "", str(e))
    except subprocess.TimeoutExpired:
        return Result(124, "", "timed out after %ss" % timeout)
    return Result(p.returncode, p.stdout, p.stderr)


def out(argv: Sequence[str], **kw: Any) -> str:
    r = run(argv, **kw)
    return r.out.strip() if r.ok else ""


def which(name: str, extra: Sequence[str] = ()) -> Optional[str]:
    """Like shutil.which, but also looks where Homebrew and friends install."""
    import shutil

    found = shutil.which(name)
    if found:
        return found
    for d in list(extra) + ["/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local/bin"),
                            str(HOME / ".cargo/bin"), str(HOME / ".bun/bin")]:
        cand = Path(d) / name
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None


def sh_quote(s: str) -> str:
    import shlex

    return shlex.quote(str(s))


# --------------------------------------------------------------------------
# admin rights


class AdminError(QuietmacError):
    def __init__(self, msg: str, out: str = ""):
        super().__init__(msg)
        self.out = out


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def admin(script: str, reason: str) -> str:
    """Run a /bin/sh script as root and return its stdout.

    In a terminal this uses sudo. Without one (an agent's shell, a LaunchAgent)
    it shows the standard macOS administrator password dialog via osascript.
    """
    log_action("ROOT (%s):\n%s" % (reason, "\n".join("    " + l for l in script.splitlines())))
    if os.geteuid() == 0:
        r = run(["/bin/sh", "-c", script])
    elif SANDBOX:
        if os.environ.get("QUIETMAC_ADMIN_AS_USER") != "1":
            raise AdminError("admin steps are disabled in the test sandbox")
        r = run(["/bin/sh", "-c", script])
    elif sys.stdin.isatty():
        try:
            p = subprocess.run(["/usr/bin/sudo", "-p", "[quietmac] password for %u (" + reason.replace("%", "%%") + "): ",
                                "/bin/sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
            r = Result(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError as e:
            r = Result(127, "", str(e))
    else:
        osa = "do shell script %s with prompt %s with administrator privileges without altering line endings" % (
            _applescript_string(script), _applescript_string("quietmac needs your password to: " + reason))
        r = run(["/usr/bin/osascript", "-e", osa])
        if r.rc != 0 and "-128" in r.err:
            raise AdminError("the administrator password dialog was cancelled")
    if not r.ok:
        raise AdminError("root step failed (%s): %s" % (reason, (r.err or r.out).strip()), r.out)
    return r.out


# --------------------------------------------------------------------------
# state


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("version", 1)
    st.setdefault("modules", {})
    st.setdefault("reaper", {"groups": {}, "custom": []})
    st.setdefault("migrations", {})
    st.setdefault("caches", {})
    return st


def save_state(st: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(STATE_FILE, json.dumps(st, indent=2, sort_keys=True) + "\n")


def atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_plist(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    with os.fdopen(fd, "wb") as fh:
        plistlib.dump(data, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, str(path))


def read_plist(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError, Exception):
        return None


# --------------------------------------------------------------------------
# defaults


_DEFAULTS_FLAG = {"boolean": "-bool", "string": "-string", "integer": "-int", "float": "-float"}


def _defaults(current_host: bool) -> List[str]:
    return ["/usr/bin/defaults"] + (["-currentHost"] if current_host else [])


def defaults_read(domain: str, key: str, current_host: bool = False) -> Optional[Tuple[str, str]]:
    """(type, value) as `defaults` reports them, or None if the key is unset."""
    t = run(_defaults(current_host) + ["read-type", domain, key])
    if not t.ok:
        return None
    typ = t.out.strip().replace("Type is ", "")
    v = run(_defaults(current_host) + ["read", domain, key])
    return typ, v.out.rstrip("\n")


def defaults_write_argv(domain: str, key: str, typ: str, value: str, current_host: bool = False) -> List[str]:
    if typ not in _DEFAULTS_FLAG:
        raise QuietmacError("cannot write defaults type %r for %s %s" % (typ, domain, key))
    if typ == "boolean":
        value = "true" if str(value).lower() in ("1", "true", "yes") else "false"
    return _defaults(current_host) + ["write", domain, key, _DEFAULTS_FLAG[typ], str(value)]


def defaults_delete_argv(domain: str, key: str, current_host: bool = False) -> List[str]:
    return _defaults(current_host) + ["delete", domain, key]


# --------------------------------------------------------------------------
# launchd


def launchctl(*args: str) -> Result:
    if SANDBOX:
        return Result(0, "", "")
    return run(["/bin/launchctl"] + list(args))


def gui(label: str) -> str:
    return "gui/%d/%s" % (UID, label)


def agent_loaded(label: str) -> bool:
    if SANDBOX:
        return (AGENTS_DIR / (label + ".plist")).exists()
    return run(["/bin/launchctl", "print", gui(label)]).ok


def agent_pid(label: str) -> Optional[int]:
    r = run(["/bin/launchctl", "print", gui(label)])
    m = re.search(r"^\s*pid = (\d+)", r.out, re.M) if r.ok else None
    return int(m.group(1)) if m else None


def launchd_job_field(target: str, field: str) -> Optional[str]:
    """A top-level field ("runs", "state", ...) from `launchctl print <target>`."""
    r = run(["/bin/launchctl", "print", target])
    if not r.ok:
        return None
    m = re.search(r"^\t%s = (.*)$" % re.escape(field), r.out, re.M)
    return m.group(1).strip() if m else None


def gui_disabled() -> Dict[str, bool]:
    r = run(["/bin/launchctl", "print-disabled", "gui/%d" % UID])
    return {m.group(1): m.group(2) == "disabled"
            for m in re.finditer(r'"([^"]+)" => (disabled|enabled|true|false)', r.out)} if r.ok else {}


def system_disabled() -> Dict[str, bool]:
    r = run(["/bin/launchctl", "print-disabled", "system"])
    return {m.group(1): m.group(2) in ("disabled", "true")
            for m in re.finditer(r'"([^"]+)" => (disabled|enabled|true|false)', r.out)} if r.ok else {}


def gui_services() -> Dict[int, str]:
    """pid -> launchd label for jobs running in this user's gui domain."""
    r = run(["/bin/launchctl", "print", "gui/%d" % UID])
    found = {}
    in_services = False
    for line in r.out.splitlines():
        if line.strip() == "services = {":
            in_services = True
            continue
        if in_services:
            if line.strip() == "}":
                break
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit() and int(parts[0]) > 0:
                found[int(parts[0])] = parts[-1]
    return found


def bootstrap_agent(label: str, plist: Path) -> None:
    """(Re)load a LaunchAgent. A job that was just booted out can make launchd
    answer "Input/output error" for a moment, so retry briefly."""
    import time

    if SANDBOX:
        return
    if not plist.exists():
        raise StepError("%s isn't installed (%s is missing)" % (label, plist))
    if agent_loaded(label):
        run(["/bin/launchctl", "bootout", gui(label)])
    last = None
    for _ in range(10):
        r = run(["/bin/launchctl", "bootstrap", "gui/%d" % UID, str(plist)])
        if r.ok:
            log_action("loaded %s" % label)
            return
        last = r
        time.sleep(0.5)
    raise StepError("launchctl bootstrap %s failed: %s" % (label, (last.err or last.out).strip()))


def agent_plist(label: str, program: Sequence[str], env: Optional[Dict[str, str]] = None,
                log_name: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "Label": label,
        "ProgramArguments": list(program),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }
    if env:
        d["EnvironmentVariables"] = dict(env)
    if log_name:
        d["StandardOutPath"] = str(LOG_DIR / (log_name + ".out.log"))
        d["StandardErrorPath"] = str(LOG_DIR / (log_name + ".err.log"))
    if extra:
        d.update(extra)
    return d


# --------------------------------------------------------------------------
# steps: every change is a list of Steps, so `plan` and `--dry-run` show
# exactly what `apply` would run.


class Step:
    """One unit of change. Exactly one of argv / script / fn is set.

    argv   - command run as the user
    script - /bin/sh snippet run as root (consecutive ones share one prompt)
    fn     - Python callable (writes files, records state)
    """

    def __init__(self, desc: str, argv: Optional[Sequence[str]] = None, script: Optional[str] = None,
                 fn: Optional[Callable[[], None]] = None, allow_fail: bool = False, show: Optional[str] = None):
        self.desc, self.argv, self.script, self.fn = desc, argv, script, fn
        self.allow_fail, self.show = allow_fail, show

    def render(self) -> str:
        if self.argv is not None:
            return "%s\n      $ %s" % (self.desc, " ".join(sh_quote(a) for a in self.argv))
        if self.script is not None:
            body = "\n".join("      # " + l for l in self.script.strip().splitlines())
            return "%s %s\n%s" % (self.desc, yellow("[root]"), body)
        if self.show and "\n" in self.show:
            return self.desc + "\n" + self.show
        return self.desc + (("\n      " + self.show) if self.show else "")


class StepError(QuietmacError):
    pass


def run_steps(steps: Sequence[Step], reason: str, dry_run: bool = False) -> None:
    i = 0
    while i < len(steps):
        s = steps[i]
        if s.script is not None:
            group = [s]
            j = i + 1
            while j < len(steps) and steps[j].script is not None:
                group.append(steps[j])
                j += 1
            parts = ["set -e"]
            for g in group:
                body = g.script.strip()
                if g.allow_fail:
                    body = "{ %s\n} || true" % body
                parts.append("# %s\n%s" % (g.desc, body))
            script = "\n".join(parts) + "\n"
            for g in group:
                say("  - " + g.desc + " " + yellow("[root]"))
            if not dry_run:
                admin(script, reason)
            i = j
            continue
        say("  - " + s.desc)
        if not dry_run:
            if s.argv is not None and SANDBOX and os.environ.get("QUIETMAC_SANDBOX_EXEC") != "1":
                log_action("SANDBOX skipped: %s" % " ".join(sh_quote(a) for a in s.argv))
            elif s.argv is not None:
                r = run(s.argv)
                log_action("RUN %s -> rc=%d" % (" ".join(sh_quote(a) for a in s.argv), r.rc))
                if not r.ok and not s.allow_fail:
                    raise StepError("%s failed: %s" % (s.desc, (r.err or r.out).strip() or "rc=%d" % r.rc))
            elif s.fn is not None:
                try:
                    s.fn()
                except QuietmacError:
                    raise
                except Exception as e:  # surface, don't hide
                    if not s.allow_fail:
                        raise StepError("%s failed: %s" % (s.desc, e))
        i += 1


# --------------------------------------------------------------------------
# prompts


def confirm(question: str, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise QuietmacError("this needs confirmation; re-run with --yes once the user has agreed")
    hint = "[Y/n]" if default else "[y/N]"
    ans = input("%s %s " % (question, hint)).strip().lower()
    return default if not ans else ans in ("y", "yes")


def choose(question: str, options: Sequence[str], default: int = 0) -> int:
    say(bold(question))
    for i, o in enumerate(options, 1):
        say("  %d) %s%s" % (i, o, dim("  (default)") if i - 1 == default else ""))
    while True:
        ans = input("> ").strip()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans) - 1


# --------------------------------------------------------------------------
# misc


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def parse_cputime(s: str) -> float:
    """ps `time`/`cputime` ("1:02.34" or "1:02:03.45") -> seconds."""
    s = s.strip()
    if not s:
        return 0.0
    parts = s.replace("-", ":").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def uptime_seconds() -> float:
    m = re.search(r"sec = (\d+)", out(["/usr/sbin/sysctl", "-n", "kern.boottime"]))
    if not m:
        return 0.0
    import time

    return max(1.0, time.time() - int(m.group(1)))


def valid_username(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", name))
