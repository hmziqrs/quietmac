# quietmac

Lower idle CPU and longer battery life on macOS, with SIP left on. Every
change is a module you can preview, apply and undo.

Measured on a MacBook Pro M3 Max running macOS 27:

| | Before | After |
|---|---|---|
| Neural Engine compiler (kept busy by `mediaanalysisd`) | 95% of a core | 0% |
| Wireless and Continuity daemons | ~11% | 1.2% |
| `sysmond` (Activity Monitor's collector) | 34% | 0% (closed) |
| Whole machine at idle | | 97.6% idle across 16 cores |

## Quick start

```sh
xcode-select -p || xcode-select --install   # the only prerequisite
./bin/quietmac detect                       # what this Mac has (read-only)
./bin/quietmac setup                        # asks what you use, shows the plan, applies your choices
```

Using a coding agent? Point it at [INSTALL.md](INSTALL.md).

## Compared with the alternatives

| Approach | What goes wrong | What quietmac does instead |
|---|---|---|
| `launchctl disable` (most guides) | On macOS 26/27, launchd ignores it for SIP-protected Apple agents. `appstoreagent` was "disabled" and still started at every boot. | A LaunchAgent kills those daemons as soon as they appear. Works with SIP on. |
| Disable SIP, then `launchctl bootout` | Works, but switches off macOS's system protection and needs a reboot into Recovery. | Never needs SIP off. |
| `killall` in a loop or cron (e.g. "kill fseventsd every few hours") | Respawn churn: each kill buys a cold start. Root daemons come straight back, and watchers lose their history. | A measured never-kill list (chronod, Siri AI, fseventsd, ...). If a daemon keeps respawning, it's killed only while it uses CPU. |
| [mds-bye](https://github.com/rayhanadev/mds-bye) | Re-runs every 5 minutes. Its bootout/disable of protected jobs fails with SIP on. It deletes Spotlight indexes and AI model packs and replaces system bundles (needs SIP's file protection off); its uninstaller can't restore the model packs. | Apple Intelligence and Siri are turned off with a configuration profile, Apple's supported switch. Nothing is deleted; `revert` removes it. |
| removemacai profile | Turns off Apple Intelligence well. It doesn't cover daemons that start anyway, Continuity, power settings or WARP. | Uses the same restriction keys, recognises an installed profile, and covers the rest. |
| [repos-volume.sh](https://github.com/elithrar/dotfiles/blob/main/repos-volume.sh) | Creates the volume, but refuses if files are already there; moving projects is manual. | Same volume design, plus migration that verifies the copy, keeps a backup and can roll back. Moves bun/pnpm caches (clones and hard links only work within one volume) and keeps Claude Code project history. |
| Flipping settings by hand | No record, no undo, and macOS updates silently reset some of them. | Records previous values; `revert` restores them, and `doctor` flags anything an update reset. |

What it does differently:

- **Asks first.** `plan` prints every command, including root scripts, before
  anything runs.
- **Exactly reversible.** It records the previous value of everything it
  changes, and logs every change to `~/Library/Logs/quietmac/actions.log`.
- **Measured, not folklore.** Each module says what it saved. Things that
  backfired in testing are blocked.
- **Cheap to run.** The reaper reads processes through libproc and looks up
  only new ones: ~1 ms of CPU per 5 s scan (0.02% of a core), against ~57 ms
  for spawning `ps`. The WARP watcher reacts to
  events and uses 0% CPU.

## Limits

- Killing isn't an off switch. A daemon gets a few seconds of life at each
  boot before the reaper ends it.
- Root daemons (`appstored`, `ANECompilerService`, ...) are out of reach.
  quietmac stops the per-user processes that drive them instead.
- Built and tested on one Mac (M3 Max, macOS 27.0). On other releases,
  `quietmac status` shows what actually took effect.
- The dev volume isn't covered by FileVault.

## Docs

- [INSTALL.md](INSTALL.md): a step-by-step install script for agents
- [docs/MODULES.md](docs/MODULES.md): every module, what it changes, and its undo
- [docs/DEV-VOLUME.md](docs/DEV-VOLUME.md): the dev volume, migration and caches
- [docs/BACKGROUND.md](docs/BACKGROUND.md): what was measured, and what didn't work
- [docs/BATTERY.md](docs/BATTERY.md): battery drain history, and what each change saves in watts
- [docs/REFERENCE.md](docs/REFERENCE.md): commands, layout, tests
