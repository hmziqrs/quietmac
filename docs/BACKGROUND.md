# Background: what was measured, and why quietmac works this way

quietmac came out of one session tuning a 16-core MacBook Pro M3 Max on
macOS 27.0 (26A428) for idle CPU and battery life, with SIP left on. This is
what was found, in order, so you can check the reasoning, or redo it when
macOS changes.

## Starting point

- `ANECompilerService` (root, the Neural Engine compiler) was at **95% of a
  core**. It was busy on behalf of `mediaanalysisd` (Photos and media
  analysis). Once mediaanalysisd was killed, ANECompilerService went to 0%,
  and on the next boot it didn't run at all.
- `appstoreagent` was "disabled" (`launchctl print-disabled` said so) and
  started anyway, at every boot.
- Activity Monitor was open. Its data collector `sysmond` (root) used
  **34% of a core**, more than every other background process combined.

## Why `launchctl disable` doesn't work any more

`launchctl print gui/$UID/com.apple.appstoreagent` shows
`properties = ... system service | ... | tle system`. For those jobs on macOS
26/27:

- `launchctl bootout gui/$UID/<label>` returns
  `150: Operation not permitted while System Integrity Protection is engaged`.
- `launchctl disable` writes the override, but launchd still starts the job
  on demand (XPC event or mach lookup). Some older-style agents do honor it,
  e.g. `com.apple.Siri.agent`.

Short of disabling SIP, the only lever is killing the process when it
appears. These daemons declare `supports pressured exit` and are jetsam
managed, so SIGKILL is what the kernel does to them anyway under memory
pressure. That's the reaper.

## Respawn churn, and why the reaper has a churn guard

Killing a daemon that a live client keeps calling is worse than leaving it
alone, because every kill buys a full cold start. Measured with
`launchctl print ... runs =` over a minute:

| Target | Result |
|---|---|
| "Siri AI" app (`com.apple.campo`) | relaunched every ~10 s (runs 12→18 in 60 s); each relaunch also brought back siriinferenced and mediaanalysisd |
| siriinferenced | looped while Siri AI runs |
| chronod (widget host) | relaunched every ~5 s (runs 3→11 in 75 s) |
| the rest (appstoreagent, the Siri helpers, siriactionsd, duetexpertd, photoanalysisd, visualintelligenced, app widget extensions) | stayed dead (`runs` unchanged) |

So the reaper counts kills per name. The first version gave up on a name
after 10 kills in 10 minutes. That turned out to be wrong for
mediaanalysisd. After one reboot it churned, the guard gave up, and over the
next 4.5 hours mediaanalysisd used **4 min 38 s** of CPU and
ANECompilerService another **4 min 37 s**. Giving up assumed the respawning
daemon would sit idle. This one worked hard.

The current reaper switches such a name to **watch mode** instead. The
process may stay alive while idle, and is killed only after 20 s above 10%
of a core. It gives up only if even that loops. A test with two respawning
dummy processes (one idle, one spinning) behaved exactly that way.

## The reaper's own cost

The first reaper spawned `ps -axo pid,uid,comm` every 5 s. Measured at that
5 s poll, each scan costs **~57 ms of CPU** (`ps` reads every process's
arguments): 1.1% of a core around the clock. It never showed up in the
reaper's own CPU time, because the cost lands on the `ps` processes.

The current reaper reads the process table through libproc
(`proc_listallpids`, `proc_pidinfo`, `proc_pidpath`) inside its own
process. It looks up only processes it hasn't seen before: **~1.05 ms per
scan, 0.02% of a core**, about 55× less. A per-process lookup of all ~900
processes on every scan cost ~4 ms, because each 5 s wake-up starts from a
cold CPU.

A pid counts as settled only once it looks the same on two consecutive
scans. launchd starts a daemon as a root `xpcproxy` process that then
exec()s into the daemon under the same pid, so a first look can be
misleading. Everything is re-checked every 5 minutes, and whenever the kill
list changes.

Two libproc details matter:

- The kernel's process name (`p_comm`) is cut to 15 characters. Longer names
  are confirmed with `proc_pidpath`.
- For a symlinked executable, `p_comm` is the target's name. Real daemons
  aren't symlinks; test dummies must be real binaries. A copied Apple binary
  doesn't work either: code signing kills it (exit 137).

## Wireless and Continuity

Idle sample, Activity Monitor closed, % of one core:

| | before | after |
|---|---|---|
| bluetoothd | 2.8 | 0.0 |
| Wi-Fi driver (`com.apple.DriverKit-AppleBCMWLAN`) | 2.4 | 0.8 |
| rapportd | 1.7 | 0.4 |
| nearbyd, sharingd, milod, wifip2pd, UniversalControl | ~4.7 | ~0.0 |
| **total** | **~11** (later ~7) | **1.2** |

These daemons have many clients that reconnect, so killing them would
churn. The fix is the settings themselves: Handoff, AirDrop "No One",
Universal Control, Bluetooth. Each preference key was checked against the
macOS 27 binaries with `strings`, so the right key is written, not one from
an old blog post. `awdl0` (peer-to-peer Wi-Fi) stays up for AirPlay and
Sidecar, and costs nothing when unused.

## Cloudflare WARP

The consumer WARP daemon is a KeepAlive root LaunchDaemon that keeps doing
health checks and captive-portal probes while disconnected (0.7-1.5% of a
core). Gotchas found while making it run on demand:

- WARP is `LSUIElement` (menu bar only), so
  `NSWorkspace.didLaunchApplicationNotification` and the matching quit
  notification are never delivered for it. Key-value observing
  `NSWorkspace.shared.runningApplications`, inside a UI-less
  `NSApplication` (activation policy `.prohibited`), works.
- The app re-adds a legacy login item every time it launches.
- AppleScript `quit app "Cloudflare WARP"` returns error -128, but the app
  does quit.
- Starting and stopping the daemon needs root. A sudoers rule that lists
  exact command lines, NOPASSWD, keeps every other `sudo` asking for a
  password (tested).

## Where the CPU went afterwards

97.5-97.65% idle across 16 cores. A minute after boot, only `talagentd`
(app restore, 1.6%, briefly) was above 1% among daemons. What was left came
from the user's own session: Terminal, the agent, and WindowServer
redrawing them.

## fseventsd and the dev volume

fseventsd averaged 0.48% of a core over the first 5 hours after boot. The
dev volume idea (`no_log` plus Spotlight off on a separate APFS volume)
comes from people running many agents in parallel, who saw fseventsd climb
over days. Verified before building it: live FSEvents delivery keeps
working on a `no_log` volume (`fs.watch`: 41 vs 42 events). See
[DEV-VOLUME.md](DEV-VOLUME.md).

## Apple Intelligence: profile versus deletion

The test Mac already had Apple Intelligence off through a configuration
profile, removemacai (`io.github.omlahore.removemacai`). It uses
`com.apple.applicationaccess` restrictions (`allowGenmoji`,
`allowWritingTools`, `allowImagePlayground`, ...), plus a MobileAsset trick
that points AI model downloads at `127.0.0.1`. That's the supported route,
and quietmac's `privacy-profile` uses the same keys.

**rayhanadev/mds-bye** takes the other route:

- It `launchctl bootout`s and `disable`s 53 jobs. On macOS 27 with SIP on,
  bootout fails (error 150) and disable is ignored for the protected ones.
- It deletes Spotlight indexes and AI model packs, and replaces the
  foundation model bundle with an immutable empty folder. Those steps need
  SIP's filesystem protection off; with SIP on they do nothing.
- It re-applies everything every 5 minutes. That's the "kill it on a
  cadence" pattern, and it's also the respawn churn this project measured.

quietmac deliberately doesn't do any of that.

## Things that look like wins but aren't

- **Killing fseventsd periodically.** It's root, it's restarted
  immediately, and every watcher and Spotlight lose their event history and
  rescan.
- **Killing chronod, the Siri AI app or siriinferenced.** These are
  measured respawn loops (above).
- **Reaping the Continuity daemons** instead of turning the features off.
  That churns too.
- **Disabling SIP** to `bootout` system jobs. The cost is the Mac's security
  model, and the settings above get most of the way there.

## References

- elithrar/dotfiles `repos-volume.sh`: an APFS volume with no_log and
  Spotlight off, mounted at `~/repos` from fstab.
- rayhanadev/mds-bye: a periodic sweep that disables Spotlight, Siri and
  Apple Intelligence (see above for why quietmac does it differently).
- removemacai (`io.github.omlahore.removemacai`): a configuration profile
  that removes Apple Intelligence.
- `blueutil` (Homebrew `blueutil`, github.com/toy/blueutil): Bluetooth power
  from the command line.
- Apple: File System Events Programming Guide (the `.fseventsd/no_log`
  convention); `man launchctl`, `man pmset`, `man mdutil`, `man ditto`.
