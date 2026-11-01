# AGENTS.md

Instructions for coding agents working on this repository. If you were asked
to install quietmac on someone's Mac rather than change its code, follow
[INSTALL.md](INSTALL.md) instead.

## What this is

quietmac lowers idle CPU use and extends battery life on macOS without turning
off SIP. Each change it can make is a module with plan, apply and revert. The
CLI is Python 3.9 using only the standard library. The WARP watcher is Swift,
and two small helpers are shell scripts.

## Layout

```
bin/quietmac              launcher: finds /usr/bin/python3 and runs the package from the checkout
quietmac/core.py          paths, command running, admin rights, state, the Step runner
quietmac/modules/         one file per area; every module returns Steps for apply and revert
quietmac/reaper.py        manages the reaper LaunchAgent, its kill list, and `probe`
quietmac/devvol.py        dev volume create, mount, destroy
quietmac/migrate.py       copy, verify, switch and roll back project folders
quietmac/detect.py        read-only facts about the Mac
quietmac/questions.py     the setup questions (single source for `setup` and INSTALL.md)
payload/                  files installed onto the Mac: the reaper, the WARP watcher and `warp` script
tests/test_quietmac.py    unit tests; never touch the system
tests/integration.py      opt-in checks against a disk image, launchd and osascript
docs/                     MODULES, DEV-VOLUME, BACKGROUND, REFERENCE, measurements/
```

## Commands

```sh
/usr/bin/python3 -m unittest discover -s tests -v   # run before every commit
/usr/bin/python3 tests/integration.py               # when touching devvol, migrate, launchd or root scripts
/usr/bin/python3 scripts/build_llms.py              # after editing any doc listed in llms.txt
./bin/quietmac plan <module>                        # see exactly what a module would run
```

## Rules

- Never run `apply`, `revert`, `adopt`, `devvol` or `migrate` against the real
  system while testing. Set `QUIETMAC_ROOT=/some/temp/dir` to get the
  sandbox. It moves every file quietmac owns under that folder, skips
  launchctl, `defaults` and other system commands, and refuses root steps
  unless `QUIETMAC_ADMIN_AS_USER=1` is also set, which only makes sense
  against a disk image.
- Keep Python 3.9 compatibility. That rules out `match`, `X | Y` type unions
  evaluated at runtime, `str.removeprefix` on older code paths, and
  dependencies outside the standard library.
- Every change goes through `core.Step` objects, so `plan` and `--dry-run`
  show it. A module records the previous values it needs in
  `state["modules"][id]`, and `revert` restores from that record, never
  from guesses.
- Root work is a `/bin/sh` script passed to `core.admin()`, one command per
  line (`set -e` ignores failures inside `a && b`). Never stage a file
  that root later reads in a user-writable location. Let the root script
  write it, as `sudoers_script()` does.
- `NEVER_KILL` exists twice, in `payload/daemon-reaper.py` and
  `quietmac/reaper.py`. A test keeps them identical. Before adding a reap
  target, measure it with `quietmac probe <name>` and say what you saw.
- Claims in docs need a measurement behind them. Put new raw numbers in
  `docs/measurements/`.
- Docs use sentence-case headings, plain language, and no em or en dashes.
  CI fails on dashes.

## Before you finish

1. Unit tests pass, and `integration.py` too if you touched those areas.
2. `scripts/build_llms.py` has been run if docs changed. CI checks
   `llms-full.txt` is current.
3. CHANGELOG.md has a line under "Unreleased" for anything user-visible.
