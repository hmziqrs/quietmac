# Modules

Every module can be planned (`quietmac plan <id>`), applied
(`quietmac apply <id>`) and reverted (`quietmac revert <id>`). `revert`
restores the exact values `apply` recorded. A setting quietmac didn't apply
itself goes back to the macOS default.

Numbers are from a MacBook Pro M3 Max (16 cores) on macOS 27.0 (26A428),
SIP on, measured on 2026-11-01. They're "% of one core": 100% means one core
fully busy, so on a 16-core Mac the whole chip is 1600%.

## Background daemons (the reaper)

On macOS 26/27, Apple's per-user agents are SIP-protected launchd jobs
(`system service | tle system`). `launchctl bootout` fails with error 150,
and `launchctl disable` is ignored: they relaunch on demand over XPC and
mach. So quietmac runs a reaper, LaunchAgent `dev.quietmac.reaper`, that
SIGKILLs them as soon as they appear. They're jetsam-managed ("supports
pressured exit"), so SIGKILL is exactly what the kernel does to them under
memory pressure.

The reaper itself:

- reads processes through libproc and looks up only processes it hasn't
  seen before: ~1 ms of CPU per 5 s scan (0.02% of a core). Spawning `ps`
  instead costs ~57 ms per scan (1.1% of a core).
- touches only your own processes, never root ones.
- has a hard-coded never-kill list (chronod, the Siri AI app,
  siriinferenced, fseventsd, Control Center extensions, WindowServer, ...).
- has a two-stage churn guard. If a name is killed 10 times in 10 minutes, it
  switches to **watch mode**: the process may run while idle and is killed
  only after 20 s above 10% of a core. If it still loops in watch mode, the
  reaper leaves it alone until next login. Both events are logged as
  `CHURN` and `GAVE UP` lines.
- re-reads its kill list (`~/.config/quietmac/reaper.conf`) whenever it
  changes.
- logs to `~/Library/Logs/quietmac/reaper.log`.

Each module below also sets a `launchctl disable` flag on its labels. The
protected jobs ignore it, but it's harmless and some jobs (e.g. Siri.agent)
honor it. `revert` clears only the flags quietmac set.

| Module | Kills | Why | You lose |
|---|---|---|---|
| `reap-appstore` | appstoreagent | Ignored `launchctl disable`, survived reboots, and spawned App Store work. | App Store downloads and updates. Run `quietmac reaper pause` first; it resumes at next login or with `reaper resume`. |
| `reap-media-analysis` | mediaanalysisd, photoanalysisd, visualintelligenced | mediaanalysisd kept the root `ANECompilerService` at **~95%** of a core; it dropped to 0% right after the kill. | Photos People, Memories and scene search; Visual Look Up; possibly Live Text in images. |
| `reap-siri-helpers` | sirittsd (~275 MB), siriknowledged, siriappintentsd; marks Siri.agent disabled | Idle in RAM and bursty at login, even with Siri off. | Siri. The Siri AI app and siriinferenced are deliberately left alone (respawn loop). |
| `reap-shortcuts` | siriactionsd | Starts at login and on triggers. | The Shortcuts app and automations. |
| `reap-suggestions` | duetexpertd | ~10 s of CPU in the first minutes after boot. | Siri Suggestions and app predictions. Borderline: it can churn after boot. |
| `reap-widgets` | every app widget extension (`--opt names=` to choose) | 25-70 MB each; 8 of them were ~340 MB. They stay dead. | Widgets. chronod and Control Center controls stay. |

Commands: `quietmac reaper status|log|add|remove|pause|resume`, and
`quietmac probe <name>`, which kills a daemon for 90 s and counts respawns
before you add it.

Never reap these, because they're measured respawn loops: chronod
(relaunched about every 5 s), the "Siri AI" app (`com.apple.campo`,
relaunched about every 10 s, and each relaunch drags siriinferenced and
mediaanalysisd back), and siriinferenced.

Watch mode exists because giving up is expensive. A reaper that stopped
killing mediaanalysisd 10 minutes after a reboot let it and the root
ANECompilerService use 4 min 38 s and 4 min 37 s of CPU over the next
4.5 hours.

`nc-widgets-remove` (guided): remove the widgets from Notification Center
and the desktop, so chronod, which can't be killed, has nothing to refresh.

## Wireless and Continuity

Idle, these daemons together used ~7-11% of a core: bluetoothd, the Wi-Fi
driver, rapportd, nearbyd, sharingd, milod, wifip2pd and UniversalControl.
With all four modules on, they used ~1.2%, mostly the Wi-Fi driver. Killing
these daemons instead backfires: clients reconnect and launchd restarts
them. The keys were checked with `strings` against the macOS 27 binaries.

| Module | Setting | Notes |
|---|---|---|
| `handoff-off` | `defaults -currentHost write com.apple.coreservices.useractivityd ActivityAdvertisingAllowed/ActivityReceivingAllowed -bool false`, then restart useractivityd | Same as the System Settings toggle. |
| `airdrop-off` | `com.apple.sharingd DiscoverableMode -string Off`, then restart sharingd | "No One". Sending still works. |
| `universal-control-off` | `com.apple.universalcontrol Disable -bool true` | UniversalControl ignores SIGTERM. It idles at 0% until next login. |
| `bluetooth-off` | `blueutil -p 0` (installs `blueutil` with Homebrew) | Stays off across reboots (verified). Refuses on a desktop Mac with a paired Bluetooth keyboard or mouse unless `--opt force=1`. |

## Apps

`warp-on-demand`: Cloudflare WARP (the consumer 1.1.1.1 app) installs a
KeepAlive root daemon that runs health checks and captive-portal probes even
while disconnected (0.7-1.5% of a core). Its app re-adds a login item every
time it starts.

- The daemon is `launchctl disable`d, so it doesn't start at boot.
- `dev.quietmac.warp-ondemand` is a Swift watcher (0% CPU, ~35 MB). It
  observes `NSWorkspace.runningApplications` with KVO. WARP is an
  LSUIElement app, so the usual didLaunch/didTerminate notifications never
  arrive for it.
  - When you open WARP, it enables and loads the daemon.
  - When you quit it, it disconnects, unloads and disables the daemon, and
    deletes the login item.
  - A launch within 90 s of login counts as an auto-start, and the app is
    quit.
- `/etc/sudoers.d/quietmac-warp` allows exactly four `launchctl` command
  lines without a password. Other `sudo` commands still ask. Root writes
  the rule itself, into a root-only temporary file, and validates it there
  before installing it. The rule never passes through a file your own
  programs could edit between the check and the install.
- `warp on|off|status` does the same from a terminal.

Revert removes all of it and gives WARP its normal always-on daemon back.

## System

| Module | Change | You lose |
|---|---|---|
| `spotlight-off` | `mdutil -a -i off`. Revert re-enables the volumes that were on before. | File search in Spotlight, Finder and Mail, and in apps that use the index. To keep search but skip code, use the dev volume instead. |
| `low-power-on-battery` | `pmset -b lowpowermode 1` | Some peak speed on battery. The biggest single battery win. |
| `powernap-off-on-battery` | `pmset -b powernap 0` | Mail, iCloud and Time Machine don't update while asleep on battery. |
| `display-sleep-on-battery` | `pmset -b displaysleep N` (`--opt value=N`, default 5) | `displaysleep 0` means the display never turns off on battery. |

## Apple Intelligence and privacy

`privacy-profile` writes a configuration profile (`.mobileconfig`), opens
it, and opens System Settings > General > Device Management, where you
click Install. A profile is Apple's supported switch for this and works with
SIP on. Pick groups with `--opt groups=a,b`:

| Group | Turns off |
|---|---|
| `apple-intelligence` | Writing Tools, Genmoji, Image Playground, Image Wand, Mail/Safari/Notes/Messages summaries, smart replies, math solving, ChatGPT and other external model integrations |
| `ai-model-downloads` | Downloading AI model packs, via MobileAsset download URL overrides. Existing models stay until Apple Intelligence is off. |
| `siri` | Siri, "Hey Siri", the menu-bar item, Siri server logging |
| `spotlight-web` | Spotlight and Look Up internet suggestions |
| `analytics` | Mac Analytics, plus "Improve Siri & Dictation" and "Improve Search" |
| `ads` | Apple personalized ads |

The keys are the `com.apple.applicationaccess` restrictions that macOS 15+
honors for manually installed profiles. They match removemacai
(`io.github.omlahore.removemacai`). If another profile already enforces a
group, `quietmac status` says so, and there's nothing to install.

Revert: `profiles remove -identifier dev.quietmac.privacy`, or remove it in
System Settings.

## Development

`dev-volume`: an APFS volume for code and build caches, with fseventsd's
event log and Spotlight off on it. Commands: `quietmac devvol`,
`quietmac migrate` and `quietmac cache`. The full design and manual
recovery are in [DEV-VOLUME.md](DEV-VOLUME.md).

## Habits

`activity-monitor-habit` (guided): quit Activity Monitor when you're done.
While it's open, its collector `sysmond` used ~34% of a core, more than
every other background process combined. On the test Mac it had used 55
minutes of CPU by early afternoon, from being left open.
