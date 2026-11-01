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
        self.assertLess(script.index("QUIETMAC_UUID="), script.index("FSTAB\""))  # uuid printed before fstab edits

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
