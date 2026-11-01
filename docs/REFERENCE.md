# Reference

Command list, layout, language choice and tests. The short overview is in the
[README](../README.md).

## Commands

```
quietmac detect [--json]          what this Mac has and how it's set (read-only)
quietmac list                     every module and whether it's in effect
quietmac questions [--json]       the setup questions, filtered for this Mac
quietmac setup                    interactive: questions -> plan -> apply
quietmac plan <ids> [--opt k=v]   explain modules and print every command, including root scripts
quietmac apply <ids> [--yes]      apply (records previous values)
quietmac revert <ids>             undo, restoring the recorded values
quietmac status                   what's applied, and drift
quietmac doctor [--fix]           prerequisites, and drift after macOS updates
quietmac measure [--since-boot]   CPU now, fseventsd's cost, reaper health
quietmac battery [--json]         battery drain history from the power log (last ~7 days)
quietmac reaper status|log|add|remove|pause|resume
quietmac probe <name>             will this daemon stay dead if reaped?
quietmac devvol status|create|mount|destroy
quietmac migrate plan|run|status|rollback|cleanup <folders>
quietmac cache list|move|restore <names>
quietmac adopt                    take over a hand-made setup from before quietmac
```

## Modules

| Area | Modules |
|---|---|
| Background daemons | `reap-appstore`, `reap-media-analysis`, `reap-siri-helpers`, `reap-shortcuts`, `reap-suggestions`, `reap-widgets`, `nc-widgets-remove` |
| Wireless and Continuity | `handoff-off`, `airdrop-off`, `universal-control-off`, `bluetooth-off` |
| Apps | `warp-on-demand` |
| System | `spotlight-off` |
| Power (battery) | `low-power-on-battery`, `powernap-off-on-battery`, `display-sleep-on-battery` |
| Apple Intelligence and privacy | `privacy-profile` (groups: apple-intelligence, ai-model-downloads, siri, spotlight-web, analytics, ads) |
| Development | `dev-volume`, plus `quietmac migrate` and `quietmac cache` |
| Habits | `activity-monitor-habit` |

What each one changes, why, what you lose, and how it's undone:
[MODULES.md](MODULES.md). The dev volume and safe migration:
[DEV-VOLUME.md](DEV-VOLUME.md).

## Principles

SIP stays on. Where launchd won't let a job be disabled, the reaper kills it on
sight instead. It only touches your own processes, keeps a never-kill list, and
backs off when something keeps relaunching a daemon.

Every change is a list of steps, so `plan` and `--dry-run` can print the exact
commands, with every root script in full. Nothing changes without confirmation,
or `--yes` once you've agreed.

`apply` records previous values in `~/.local/state/quietmac/state.json`, which
is how `revert` restores them. Every change, root scripts included, is also
appended to `~/Library/Logs/quietmac/actions.log`.

Each module states what it saved on a real machine, and
[BACKGROUND.md](BACKGROUND.md) lists what was tried and rejected.

The reaper costs about 1 ms of CPU per 5-second scan, and the WARP watcher only
wakes when an app launches or quits.

## Why Python, Swift and sh

The tool and the reaper are Python 3, standard library only.
`/usr/bin/python3` comes with the Xcode command line tools, which every
developer Mac already has because git needs them, so there's nothing to
install. Python also handles plists, JSON, careful file copies and state
well. The code targets 3.9, the version the command line tools ship.

The WARP watcher is Swift, because it needs AppKit key-value observing to
react to an app launching or quitting without polling. It's compiled on
install with `swiftc`, which is also part of the command line tools.

The `warp on|off|status` command and the `bin/quietmac` launcher are shell
scripts: the first for instant start-up, the second so it runs on macOS's
bash 3.2.

Node or bun would need installing first, and a compiled CLI would need a
build step just to read a plist.

## Layout

```
bin/quietmac            launcher (finds python3, runs the package from this checkout)
quietmac/               the CLI: core, detect, questions, modules/, devvol, migrate, cache, reaper, measure
payload/                installed onto the Mac: daemon-reaper.py, warp-ondemand.swift, warp, sudoers template
docs/                   MODULES, DEV-VOLUME, BACKGROUND
tests/                  unit tests (no system changes)
INSTALL.md              the agent's install script
```

Installed files live outside the checkout, so moving or deleting the
repository breaks nothing:

- `~/.local/share/quietmac/`: reaper, watcher, profile
- `~/.config/quietmac/reaper.conf`
- `~/.local/state/quietmac/state.json`
- `~/Library/LaunchAgents/dev.quietmac.*.plist`
- `~/Library/Logs/quietmac/`

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -v   # fast, never touches the system
/usr/bin/python3 tests/integration.py               # opt-in, ~1 minute: real macOS machinery, cleaned up after
```

`tests/integration.py` uses a throwaway APFS container inside a disk image,
a temporary LaunchAgent and osascript without admin rights. It checks:

- dev-volume create and destroy, including the safety refusals
- migration: a failed switch, resume, rollback and cleanup
- module error handling and partial reverts
- the LaunchAgent restart helper
- the sudoers script's round trip through AppleScript

The unit tests never touch the system. The dev volume, migration, rollback,
cleanup and destroy were also tested end to end against an APFS container
inside a throwaway disk image, with `QUIETMAC_ROOT` set (a sandbox where
quietmac's files live in one folder and system commands are skipped).
