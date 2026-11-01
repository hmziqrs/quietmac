# quietmac

Lower idle CPU and longer battery life on macOS, without disabling SIP.

quietmac is a set of small, reversible **modules**: kill the Apple daemons
you don't use, turn off Continuity features you don't need, run Cloudflare
WARP only on demand, restrict Apple Intelligence with a configuration
profile, tune battery power settings, and move code onto a volume where
fseventsd and Spotlight leave it alone. Each module asks first, shows exactly
what it will run, records what it changed, and can be reverted.

On the Mac it was built on (MacBook Pro M3 Max, macOS 27, SIP on):

- **Neural Engine compiler** (`ANECompilerService`): 95% of a core → 0%,
  by killing mediaanalysisd.
- **Wireless and Continuity daemons**: ~11% → 1.2% of a core.
- **Activity Monitor's collector**: 34% → 0%, by quitting it.
- **Idle**: 97.6% across 16 cores, with what's left mostly the user's own
  terminal and agent.

The details, and what didn't work, are in
[docs/BACKGROUND.md](docs/BACKGROUND.md).

## Quick start

```sh
xcode-select -p || xcode-select --install
git clone <this repo> quietmac && cd quietmac
./bin/quietmac detect      # what this Mac has; read-only
./bin/quietmac setup       # asks what you use, shows the plan, applies what you choose
```

Using a coding agent? Point it at **[INSTALL.md](INSTALL.md)**. It's a
step-by-step script for the agent: detect, ask the user in batches, show the
exact plan, apply, verify.

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
[docs/MODULES.md](docs/MODULES.md). The dev volume and safe migration:
[docs/DEV-VOLUME.md](docs/DEV-VOLUME.md).

## Principles

- **SIP stays on.** Where launchd won't let a job be disabled, the reaper
  kills it on sight. It touches only your own processes, has a never-kill
  list, and backs off when something keeps relaunching a daemon.
- **Ask, show, then do.** Every change is a list of steps. `plan` and
  `--dry-run` print the exact commands, and every root script in full.
  Nothing changes without confirmation (or `--yes` after you agreed).
- **Reversible.** `apply` records previous values in
  `~/.local/state/quietmac/state.json`. Every change, including root
  scripts, is appended to `~/Library/Logs/quietmac/actions.log`.
- **Measured, not folklore.** Each module states what it saved on a real
  machine, and the docs list what was tried and rejected.
- **Cheap to run.** The reaper costs ~1 ms of CPU per 5 s scan, and the WARP
  watcher is event-driven (0% CPU).

## Why Python, Swift and sh

- **Python 3 (standard library only)** for the tool and the reaper.
  `/usr/bin/python3` comes with the Xcode command line tools, which every
  developer Mac has (git needs them), so there's nothing to install. It also
  handles plists, JSON, careful file copies and state well. The code
  targets 3.9, the version the command line tools ship.
- **Swift** for the WARP watcher. It needs AppKit key-value observing to
  react to an app launching or quitting with zero polling. It's compiled
  on install (`swiftc`, part of the command line tools).
- **POSIX sh** for the `warp on|off|status` command (instant start-up) and
  the `bin/quietmac` launcher (works with macOS's bash 3.2).

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
/usr/bin/python3 -m unittest discover -s tests -v
```

The unit tests never touch the system. The dev volume, migration, rollback,
cleanup and destroy were also tested end to end against an APFS container
inside a throwaway disk image, with `QUIETMAC_ROOT` set (a sandbox where
quietmac's files live in one folder and system commands are skipped).

## Credits and references

- elithrar/dotfiles `repos-volume.sh`: the dev volume idea.
- rayhanadev/mds-bye: a different approach to disabling Spotlight, Siri and
  AI; [docs/BACKGROUND.md](docs/BACKGROUND.md) explains why quietmac doesn't
  delete system files.
- removemacai (`io.github.omlahore.removemacai`): the restriction keys used
  by `privacy-profile`.
- blueutil (github.com/toy/blueutil).
