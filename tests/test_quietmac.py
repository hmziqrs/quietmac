"""Unit tests: pure logic only. They never touch the real system.

Run: /usr/bin/python3 -m unittest discover -s tests -v   (from the repo root)
"""

import importlib.util
import os
import plistlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["QUIETMAC_ROOT"] = tempfile.mkdtemp(prefix="quietmac-test-")  # sandbox before importing
sys.path.insert(0, str(ROOT))

from quietmac import cli, core, devvol, migrate, questions, reaper  # noqa: E402
from quietmac.modules import ALL, BY_ID, privacy  # noqa: E402
from quietmac.modules.continuity import DefaultsModule  # noqa: E402


def comm_of(pid):
    import subprocess as sp
    return os.path.basename(sp.run(["/bin/ps", "-o", "comm=", "-p", str(pid)], stdout=sp.PIPE,
                                   universal_newlines=True).stdout.strip())


def load_payload():
    spec = importlib.util.spec_from_file_location("payload_reaper", str(ROOT / "payload" / "daemon-reaper.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReaperTests(unittest.TestCase):
    def test_never_kill_lists_match(self):
        self.assertEqual(load_payload().NEVER_KILL, reaper.NEVER_KILL)

    def test_never_kill_has_the_measured_loopers(self):
        for n in ("chronod", "Siri AI", "siriinferenced", "fseventsd", "WindowServer",
                  "BluetoothSettingsAppIntentsWidgetExtension"):
            self.assertIn(n, reaper.NEVER_KILL)

    def test_conf_round_trip(self):
        st = core.load_state()
        st["reaper"]["groups"] = {"reap-appstore": ["appstoreagent"], "reap-x": ["b", "a"]}
        st["reaper"]["custom"] = ["zzz"]
        text = reaper.render_conf(st)
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(text + "\nchronod  # never-kill names are dropped by the reaper\n")
        names = load_payload().read_names(fh.name)
        self.assertEqual(names, frozenset(["appstoreagent", "a", "b", "zzz"]))

    def test_check_name(self):
        self.assertIsNotNone(reaper.check_name("chronod"))
        self.assertIsNotNone(reaper.check_name("/usr/libexec/duetexpertd"))
        self.assertIsNone(reaper.check_name("qm-no-such-process-xyz"))

    def test_libproc_lister_works_here(self):
        lister = load_payload().make_lister()
        self.assertEqual(lister.method, "libproc")
        self.assertIsNotNone(lister.cpu_seconds(os.getpid()))


class AuditFixTests(unittest.TestCase):
    def test_inside_respects_path_boundaries(self):
        self.assertTrue(migrate.inside("/Users/me/os", "/Users/me/os"))
        self.assertTrue(migrate.inside("/Users/me/os/x", "/Users/me/os"))
        self.assertFalse(migrate.inside("/Users/me/os2", "/Users/me/os"))

    def test_sudoers_written_by_root_and_validated_before_install(self):
        from quietmac.modules import warp
        script = warp.sudoers_script(warp.sudoers_text())
        self.assertNotIn(str(core.STATE_DIR), script)  # no user-writable staging file
        self.assertIn("mktemp", script)
        self.assertLess(script.index("visudo -cf"), script.index("install -m 0440"))
        with self.assertRaises(core.QuietmacError):
            warp.sudoers_script("x\nQUIETMAC_SUDOERS\ny")

    def test_uid_listing_sees_this_process(self):
        lister = load_payload().make_lister()
        comm = comm_of(os.getpid())
        found = dict(lister.processes(frozenset([comm])))
        self.assertIn(os.getpid(), found)
        lister.forget(os.getpid())
        self.assertNotIn(os.getpid(), lister.hits)


class AuditRegressionTests(unittest.TestCase):
    """Each test fails on the code from before the audit (commit dc32fa4)."""

    def test_killed_process_is_not_returned_again(self):
        # a killed child stays a zombie until reaped; the old lister kept returning it
        import signal
        import time as _t
        r = load_payload()
        comm = comm_of(os.getpid())
        child = os.fork()
        if child == 0:
            _t.sleep(30)
            os._exit(0)
        try:
            _t.sleep(0.3)
            lister = r.make_lister()
            names = frozenset([comm])
            lister.processes(names)
            self.assertIn(child, dict(lister.processes(names)))  # settled match
            os.kill(child, signal.SIGKILL)
            _t.sleep(0.3)
            lister.forget(child)
            self.assertNotIn(child, dict(lister.processes(names)))
        finally:
            os.waitpid(child, 0)

    def test_root_processes_are_not_even_listed(self):
        lister = load_payload().make_lister()
        lister.processes(frozenset(["qm-nothing"]))
        seen = set(lister.tentative) | set(lister.settled)
        self.assertIn(os.getpid(), seen)
        self.assertNotIn(1, seen)  # launchd (root): filtered out by the kernel (PROC_UID_ONLY)

    def test_a_failing_scan_does_not_stop_the_reaper(self):
        import threading
        import time as _t
        r = load_payload()
        log = tempfile.NamedTemporaryFile(delete=False).name
        r.LOG_PATH, r.POLL = log, 0.05
        calls = []

        def flaky(*args):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("simulated libproc failure")
            return args[1], args[2]
        r.scan_once = flaky
        r.make_lister = lambda: r.PsLister()
        from unittest import mock
        r.signal = mock.Mock(SIGTERM=15, SIGKILL=9)  # signal.signal() only works on the main thread
        threading.Thread(target=r.main, daemon=True).start()
        _t.sleep(0.6)
        self.assertGreater(len(calls), 3)  # kept scanning after the failure
        self.assertIn("scan failed (OSError: simulated libproc failure); continuing", Path(log).read_text())

    def test_reaper_does_not_load_subprocess_or_traceback(self):
        import subprocess as sp
        code = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('r', %r); "
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); m.make_lister(); "
                "print(sorted(x for x in ('subprocess', 'traceback') if x in sys.modules))"
                % str(ROOT / "payload" / "daemon-reaper.py"))
        out = sp.run([sys.executable, "-E", "-s", "-S", "-c", code], stdout=sp.PIPE, universal_newlines=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_reaper_plist_uses_dash_S(self):
        self.assertEqual(reaper.plist_data()["ProgramArguments"][:4], ["/usr/bin/python3", "-E", "-s", "-S"])

    def test_battery_render_survives_a_short_unplugged_week(self):
        from quietmac import battery
        res = {"battery": {}, "capacity_wh_used": 60.0, "analysis": battery.analyze(BatteryTests.LOG[:300]),
               "totals": {"awake_h": 0.1, "awake_pct": 1.0}, "awake_percent_per_hour": None,
               "hours_per_charge_awake": None, "asleep_percent_per_hour": None}
        battery.render(res)  # the old code formatted None with %.1f here and crashed

    def test_sudo_prompt_escapes_percent(self):
        src = (ROOT / "quietmac" / "core.py").read_text()
        self.assertIn('reason.replace("%", "%%")', src)

    def test_rollback_relocates_when_checkout_is_inside(self):
        # the fix: `migrate rollback` re-runs from a temp copy if the checkout lives in the folder or its copy
        from unittest import mock
        st = core.load_state()
        st["migrations"]["/x/os"] = {"src": "/x/os", "dst": "/x/dev/os", "status": "done"}
        core.save_state(st)
        with mock.patch.object(cli, "_relocate_if_inside") as reloc, mock.patch.object(migrate, "rollback"):
            cli.main(["migrate", "rollback", "/x/os", "--yes"])
        reloc.assert_called_once_with(["/x/os", "/x/dev/os"])


    def _adopt_with(self, run_rc):
        from unittest import mock
        legacy = [{"label": "com.example.daemon-reaper", "plist": "/nonexistent/x.plist", "loaded": True,
                   "names": ["appstoreagent"]}]
        facts = {"widgets": {"installed": []}, "warp": {"installed": False, "legacy_watchers": [], "legacy_sudoers": []}}
        with mock.patch.object(cli.detect, "detect", return_value=facts), \
                mock.patch.object(reaper, "legacy_agents", return_value=legacy), \
                mock.patch.object(cli, "_run_modules", return_value=run_rc) as run, \
                mock.patch.object(core, "run_steps") as steps:
            rc = cli.main(["adopt", "--yes"])
        return rc, run, steps

    def test_adopt_keeps_the_old_reaper_when_a_module_fails(self):
        # the fix: adopt used to retire the hand-made reaper even when the new modules failed to apply
        rc, run, steps = self._adopt_with(1)
        self.assertEqual(rc, 1)
        self.assertIn("adopting=1", run.call_args[0][0].opt)
        steps.assert_not_called()

    def test_adopt_retires_the_old_reaper_on_success(self):
        rc, run, steps = self._adopt_with(0)
        self.assertEqual(rc, 0)
        self.assertEqual(steps.call_args[0][1], "retire the hand-made reaper")

class ModuleTests(unittest.TestCase):
    def test_ids_unique_and_documented(self):
        self.assertEqual(len(ALL), len(BY_ID))
        for m in ALL:
            self.assertTrue(re.match(r"^[a-z0-9-]+$", m.id), m.id)
            self.assertTrue(m.title and m.summary and m.category, m.id)

    def test_install_md_mentions_every_module(self):
        text = (ROOT / "INSTALL.md").read_text() + (ROOT / "docs" / "MODULES.md").read_text()
        for m in ALL:
            self.assertIn(m.id, text, "document %s in INSTALL.md or docs/MODULES.md" % m.id)

    def test_defaults_normalisation(self):
        self.assertEqual(DefaultsModule._norm("boolean", "false"), "0")
        self.assertEqual(DefaultsModule._norm("boolean", "1"), "1")
        self.assertEqual(DefaultsModule._norm("string", "Off"), "Off")

    def test_defaults_argv(self):
        self.assertEqual(core.defaults_write_argv("d", "k", "boolean", "true", True),
                         ["/usr/bin/defaults", "-currentHost", "write", "d", "k", "-bool", "true"])
        with self.assertRaises(core.QuietmacError):
            core.defaults_write_argv("d", "k", "dictionary", "{}")

    def test_profile(self):
        d = privacy.build_profile(list(privacy.GROUPS))
        data = plistlib.dumps(d)
        self.assertIn(b"com.apple.applicationaccess", data)
        self.assertEqual(d["PayloadIdentifier"], privacy.IDENTIFIER)
        self.assertEqual(d["PayloadUUID"], privacy.build_profile(["siri"])["PayloadUUID"])  # stable: reinstall replaces
        prefs = d["PayloadContent"][1]["PayloadContent"]
        # two groups write com.apple.assistant.support: they must be merged, not overwritten
        self.assertEqual(set(prefs["com.apple.assistant.support"]["Forced"][0]["mcx_preference_settings"]),
                         {"Assistant Enabled", "Siri Data Sharing Opt-In Status", "Search Queries Data Sharing Status"})


class CoreTests(unittest.TestCase):
    def test_parse_cputime(self):
        self.assertAlmostEqual(core.parse_cputime("4:38.08"), 278.08)
        self.assertAlmostEqual(core.parse_cputime("1:02:03.50"), 3723.5)
        self.assertEqual(core.parse_cputime(""), 0.0)

    def test_applescript_string(self):
        self.assertEqual(core._applescript_string('a "b" \\ c\nd'), '"a \\"b\\" \\\\ c\\nd"')

    def test_opts_scoping(self):
        o = cli.parse_opts(["force=1", "reap-widgets.names=A,B", "dev-volume.mount=~/x"])
        self.assertEqual(cli.opts_for(o, "reap-widgets"), {"force": "1", "names": "A,B"})
        self.assertEqual(cli.opts_for(o, "dev-volume"), {"force": "1", "mount": "~/x"})

    def test_step_runner_dry_run_runs_nothing(self):
        ran = []
        core.run_steps([core.Step("x", fn=lambda: ran.append(1)), core.Step("y", script="exit 1")], "t", dry_run=True)
        self.assertEqual(ran, [])


class DevVolumeTests(unittest.TestCase):
    def test_create_script_stops_on_any_failure(self):
        # `set -e` ignores failures inside `a && b`, so the script must not chain commands
        script = devvol.create_script("Dev", "/Users/me/dev", "disk3")
        self.assertTrue(script.startswith("set -eu\n"))
        for line in script.splitlines():
            self.assertNotIn(" && ", line, line)
        self.assertIn("rw,nobrowse,owners", script)
        # fstab is touched before anything is created (macOS 27 denies it); the uuid is printed before the append
        self.assertLess(script.index('touch "$FSTAB"'), script.index("addVolume"))
        self.assertLess(script.index("QUIETMAC_UUID="), script.index('>> "$FSTAB"'))
        self.assertIn('echo "QUIETMAC_UUID=$UUID" >&2', script)  # osascript drops stdout on failure

    def test_validate_rejects_bad_input(self):
        with self.assertRaises(core.QuietmacError):
            devvol.validate("bad name!", "/tmp/x")
        with self.assertRaises(core.QuietmacError):
            devvol.validate("Dev", "/tmp/has space")


class MigrateTests(unittest.TestCase):
    def test_claude_key(self):
        self.assertEqual(migrate.claude_key("/Users/me/Library/Application Support/x.y"),
                         "-Users-me-Library-Application-Support-x-y")

    def test_scan_and_compare(self):
        a = tempfile.mkdtemp()
        b = tempfile.mkdtemp()
        for root in (a, b):
            os.makedirs(os.path.join(root, "d"))
            Path(root, "d", "f").write_text("same")
            os.symlink("d/f", os.path.join(root, "l"))
            os.utime(os.path.join(root, "d", "f"), (1000000000, 1000000000))
        Path(a, ".DS_Store").write_text("ignored")
        ea, files, total = migrate.scan(a)
        eb, _, _ = migrate.scan(b)
        self.assertEqual((files, total), (2, 11))
        self.assertEqual(migrate.compare(ea, eb), [])
        Path(b, "d", "f").write_text("different")
        eb, _, _ = migrate.scan(b)
        self.assertTrue(migrate.compare(ea, eb))


class QuestionTests(unittest.TestCase):
    def facts(self):
        return {
            "bluetooth": {"power": True, "devices": [{"name": "AirPods", "type": "headphones", "connected": False}],
                          "devices_known": True, "has_input_device": False},
            "continuity": {"handoff": True, "airdrop": "Everyone", "universal_control": True},
            "quietmac": {"applied": []},
            "widgets": {"installed": [{"name": "WeatherWidget", "app": "Weather", "path": "/x"}], "running": []},
            "spotlight": {"data_volume_enabled": True},
            "macos": {"laptop": True},
            "power": {"battery": {"lowpowermode": "0", "displaysleep": "0", "powernap": "1"}},
            "warp": {"installed": True, "watcher_loaded": False},
            "dev": {"projects": [{"path": "/Users/me/os", "repos": 3}],
                    "caches": {"bun": {"exists": True, "why": "w", "required_with_devvol": True}}},
            "devvol": {"exists": False},
            "fseventsd": {"percent_of_core": 0.5},
        }

    def test_all_questions_evaluate(self):
        qs = questions.evaluate(self.facts())
        self.assertTrue(all(q["ask"] for q in qs if q["id"] not in ("apple-intelligence",)))
        for q in qs:
            self.assertLessEqual(len(q["header"]), 12, q["id"])
            self.assertGreaterEqual(len(q["options"]), 1, q["id"])
            for o in q["options"]:
                for mid in o.get("apply", []):
                    self.assertIn(mid, BY_ID, "%s -> %s" % (q["id"], mid))

    def test_skips_when_already_off(self):
        f = self.facts()
        f["bluetooth"]["power"] = False
        f["warp"]["installed"] = False
        asked = {q["id"]: q["ask"] for q in questions.evaluate(f)}
        self.assertFalse(asked["bluetooth"])
        self.assertFalse(asked["warp"])


class BatteryTests(unittest.TestCase):
    LOG = """\
2026-10-25 10:00:00 +0500 Assertions          \tSummary- [System: PrevIdle kDisp] Using Batt(Charge: 90)
2026-10-25 11:00:00 +0500 Assertions          \tSummary- [System: PrevIdle kDisp] Using Batt(Charge: 80)
2026-10-25 11:00:05 +0500 Sleep               \tEntering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=active Using Batt (Charge:80%) 3600 secs
2026-10-25 11:00:07 +0500 Wake Requests       \t[*process=dasd request=SleepService deltaSecs=1068 wakeAt=2026-10-25 11:18:00 info="com.apple.dasd:501:com.apple.GenerativeFunctions"]
2026-10-25 12:00:05 +0500 DarkWake            \tDarkWake from Deep Idle [CDNP] : due to smc.sysState.Wake wifibt/ Using BATT (Charge:79%) 5 secs
2026-10-25 13:00:05 +0500 Wake                \tWake from Deep Idle [CDNVA] : due to UserActivity Assertion Using BATT (Charge:78%) 60 secs
2026-10-25 14:00:05 +0500 Assertions          \tSummary- [System: PrevIdle kDisp] Using Batt(Charge: 68)
2026-10-25 15:00:05 +0500 Assertions          \tSummary- [System: PrevIdle kDisp] Using AC(Charge: 70)
"""

    def test_awake_and_asleep_are_separated(self):
        from quietmac import battery
        a = battery.analyze(self.LOG)
        d = a["days"]["2026-10-25"]
        # awake: 10:00-11:00 (10%) + 13:00-14:00 (10%) + 5 s; asleep: 11:00-13:00 (2%)
        self.assertAlmostEqual(d["awake_h"], 2.0, places=2)
        self.assertAlmostEqual(d["awake_pct"], 20)
        self.assertAlmostEqual(d["asleep_h"], 2.0, places=2)
        self.assertAlmostEqual(d["asleep_pct"], 2)
        self.assertEqual(len(a["sessions"]), 1)
        self.assertEqual(a["wake_requests"][0], ("com.apple.GenerativeFunctions", 1))



if __name__ == "__main__":
    unittest.main()
