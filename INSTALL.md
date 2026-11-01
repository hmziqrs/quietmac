# Installing quietmac: a script for the agent doing it

This file is written for a coding agent (Claude Code or similar) setting up
quietmac on someone's Mac. A human can follow it too, or run
`./bin/quietmac setup`, which asks the same questions in the terminal.

quietmac never decides for the user. Your job: find out what this Mac has,
ask the user what they use, show the exact plan, apply only what they chose,
then verify it.

## Rules

1. **Ask before changing anything.** Use the questions in step 2. Apply a
   module only after the user picked it. Pass `--yes` only once they have
   agreed to that specific plan.
2. **Never disable SIP**, and never suggest it. Nothing here needs it off.
3. **Never kill or reap root-owned processes.** The reaper refuses them anyway.
4. **Never add a name from the never-kill list** to the reaper:
   chronod, "Siri AI" (launchd label `com.apple.campo`), siriinferenced,
   fseventsd, Control Center extensions and the rest of `NEVER_KILL` in
   `quietmac/reaper.py`. These were measured to respawn in loops, or the
   session depends on them.
5. **Warn the user before any step that needs their password.** In an agent's
   shell there's no terminal, so quietmac shows the standard macOS
   administrator dialog. It's one dialog per module. Say which module is
   about to ask, and why.
6. **Probe before adding an unknown daemon:** run `quietmac probe <name>`
   before `quietmac reaper add <name>`.
7. **Moving folders:** run quietmac from `~`, with nothing working inside the
   folders being moved. quietmac checks for this and refuses otherwise.
8. **Keep records.** quietmac writes its own ledger to
   `~/Library/Logs/quietmac/actions.log` and its state to
   `~/.local/state/quietmac/state.json`. If you keep notes or memory about
   the user's machine, record what you applied and how to undo it
   (`quietmac revert <id>`).

## 0. Prerequisites

```sh
xcode-select -p || xcode-select --install   # provides python3, git and swiftc
git clone https://github.com/hmziqrs/quietmac.git && cd quietmac   # anywhere is fine
./bin/quietmac doctor
```

- Python 3.9+ (the Xcode command line tools ship 3.9). No pip packages.
- Homebrew, only for `bluetooth-off` (it installs `blueutil`).
- The Swift compiler, only for `warp-on-demand` (it's part of the command
  line tools).
- Built and tested on macOS 27.0 (26A428), MacBook Pro M3 Max, SIP on.
  On older releases a module may show `n/a`, or show `partial` after apply.
  `quietmac status` tells you the truth either way.

## 1. Detect

```sh
./bin/quietmac detect           # human summary: show it to the user
./bin/quietmac detect --json    # the same facts, for you
./bin/quietmac list             # every module and whether it's in effect already
```

Read these from the JSON before asking anything:

- `reaper.legacy`, `warp.legacy_watchers`, `warp.legacy_sudoers`: a hand-made
  setup from before quietmac. If any of these is non-empty, go to
  [Adopting an existing setup](#adopting-an-existing-setup) first.
- `bluetooth.devices`: what's paired. Only listed while Bluetooth is on.
- `profiles.apple_intelligence_restricted`: a profile already handles AI.
- `daemons.running`: CPU seconds of known daemons since boot. A large
  `mediaanalysisd` value means the Neural Engine compiler has been busy too.
- `fseventsd.percent_of_core`: tells you whether the dev volume is worth it.
- `dev.projects`, `dev.caches`: candidates for the dev volume.

## 2. Ask

```sh
./bin/quietmac questions --json
```

This returns the questions **already filtered and filled in for this Mac**.
For example, there's no Bluetooth question when Bluetooth is off, and the
text lists the actual paired devices. For each question with `"ask": true`,
ask it with your question UI, using `question`, `options[].label` and
`options[].description`. Ask them in batches by `batch` number (at most 4
questions per batch). `multi: true` means the user may pick several options.
Mark `recommended` (an option index) as the recommendation, if set. Follow
`notes`.

Each option carries what to do with the answer:

- `apply`: module ids for `quietmac apply`
- `opts`: `--opt` values, e.g. `{"display-sleep-on-battery.value": "10"}`
- `migrate`: folders for `quietmac migrate run`
- `cache`: caches for `quietmac cache move`

The question bank, for reference (the source is `quietmac/questions.py`):

**Batch 1: wireless**

| id | Question | Answer → modules |
|---|---|---|
| bluetooth | Bluetooth uses 1-3% of a core with nothing connected. Paired: …. Turn it off, and switch it on from Control Center when needed? | Turn it off → `bluetooth-off` |
| handoff | Do you use Handoff or Universal Clipboard? | No → `handoff-off` |
| airdrop | Do you receive files with AirDrop? (Sending still works.) | No → `airdrop-off` |
| universal-control | Do you control an iPad or another Mac with this keyboard and trackpad? | No → `universal-control-off` |

For Bluetooth: if a Bluetooth keyboard, mouse or trackpad is paired and the
Mac has no built-in keyboard, don't recommend turning it off. The module
refuses without `--opt force=1`. Mention that AirPods and Apple Watch unlock
stop working until Bluetooth is turned back on.

**Batch 2: Apple features**

| id | Question | Answer → modules |
|---|---|---|
| siri | Do you use Siri? | No → `reap-siri-helpers` |
| apple-intelligence | Which should a configuration profile turn off? (multi) | Apple Intelligence / AI model downloads / Siri / Analytics, web, ads → `privacy-profile` with `--opt privacy-profile.groups=…` (merge every chosen group into one list) |
| shortcuts | Do you use the Shortcuts app or its automations? | No → `reap-shortcuts` |
| suggestions | Do you rely on Siri Suggestions? | No → `reap-suggestions` |

**Batch 3: background daemons**

| id | Question | Answer → modules |
|---|---|---|
| appstore | Do you install or update App Store apps often? | Rarely → `reap-appstore` (tell them: `quietmac reaper pause` before updating) |
| photos | Do you use Photos People/Memories/search, or Visual Look Up? | No → `reap-media-analysis` |
| widgets | Do you use desktop or Notification Center widgets? | No → `reap-widgets` and `nc-widgets-remove` |
| spotlight | Do you search files with Spotlight, Finder or Mail? | No → `spotlight-off` |

**Batch 4: power and apps**

| id | Question | Answer → modules |
|---|---|---|
| low-power | Low Power Mode automatically on battery? | Yes → `low-power-on-battery` |
| display-sleep | The display never turns off on battery; turn it off after N minutes? | 5 or 10 → `display-sleep-on-battery` with `--opt display-sleep-on-battery.value=N` |
| powernap | Power Nap off on battery? | Yes → `powernap-off-on-battery` |
| warp | Run Cloudflare WARP only while its app is open? | Yes → `warp-on-demand` |

**Batch 5: development**

| id | Question | Answer → |
|---|---|---|
| dev-volume | Create ~/dev with fseventsd logging and Spotlight off, and move projects there? | Yes → `dev-volume` (only if fseventsd ≥ ~2% of a core, or they run many agents or builds in parallel; otherwise recommend "Not now") |
| migrate | Which folders move onto the dev volume? (multi) | `quietmac migrate run <folders>` |
| caches | Which caches follow them? (multi) | `quietmac cache move <names>` (bun and pnpm are needed if their projects move) |

Two items aren't questions, so mention them yourself:

- `activity-monitor-habit`: while Activity Monitor is open, its data
  collector `sysmond` costs ~30% of a core. Use `quietmac measure` instead.
- `quietmac measure --since-boot` shows what's using CPU, if the user wants
  numbers before deciding.

## 3. Show the plan

```sh
./bin/quietmac plan <ids...> [--opt module.key=value ...]
```

Summarize for the user, in plain words: what changes, what they lose, and
which modules will ask for their password. `plan` prints the exact commands,
including every root script. Get a yes on the whole plan.

## 4. Apply, in this order

```sh
# a) no password needed
./bin/quietmac apply reap-media-analysis reap-siri-helpers ... handoff-off airdrop-off --yes

# b) each of these shows one macOS password dialog. Tell the user first.
./bin/quietmac apply warp-on-demand --yes
./bin/quietmac apply spotlight-off low-power-on-battery --yes

# c) the profile: quietmac opens it. The user must click Install in
#    System Settings > General > Device Management, and enter their password.
./bin/quietmac apply privacy-profile --opt privacy-profile.groups=apple-intelligence,siri --yes

# d) guided items: quietmac prints the steps for the user
./bin/quietmac apply nc-widgets-remove --yes
```

Then the dev volume, if chosen. This takes the longest, so do it last:

```sh
cd ~                                         # never from inside a folder being moved
./bin/quietmac devvol create --yes           # one password dialog; creates and mounts ~/dev
./bin/quietmac migrate plan ~/os ~/ps        # sizes, free space, and anything working inside them
#   -> ask the user to close editors, terminals, dev servers and agents working in those folders
./bin/quietmac migrate run ~/os ~/ps --yes   # copy, verify, switch; old paths become links
./bin/quietmac cache move bun go-build --yes
```

If this checkout lives inside a folder being moved, quietmac re-runs itself
from a temporary copy automatically. The agent's own shell must not be
inside those folders, though: `cd ~` first.

## 5. Verify

```sh
./bin/quietmac status        # every module in effect, plus drift
./bin/quietmac measure       # 30 s CPU sample, fseventsd cost, reaper health
./bin/quietmac reaper status # after ~2 minutes: look for CHURN / GAVE UP lines
```

- `CHURN on X`: something keeps relaunching X. The reaper now kills it only
  while it burns CPU. That's fine.
- `GAVE UP on X`: X loops even then. Suggest `quietmac revert <module>` or
  `quietmac reaper remove X`.
- Offer a reboot test. After logging back in, run `quietmac status`. The
  reaper should have killed its targets within seconds, and launchd shows
  `runs = 1` for them.

## 6. Afterwards

- Run `./bin/quietmac doctor` after every macOS update. Updates can reset
  settings, rename daemons or reload agents; re-apply whatever it lists.
- `./bin/quietmac revert <id>` undoes a module and restores the exact values
  it recorded.
- To kill another daemon, run `quietmac probe <name>` first, then
  `quietmac reaper add <name>`.
- Once the user has worked from the dev volume for a while,
  `quietmac migrate cleanup` deletes the original copies.

## Adopting an existing setup

If `detect` reports a legacy reaper or WARP watcher (a hand-made
`com.<someone>.daemon-reaper`, `/etc/sudoers.d/warp-ondemand`, or a `warp`
script):

```sh
./bin/quietmac adopt --dry-run   # shows the mapping: names -> modules, and what gets retired
./bin/quietmac adopt --yes       # applies the equivalent modules, stops the old agents, backs up their files
```

`adopt` maps the old kill list onto modules (`reap-*`, with `reap-widgets`
for widget names). Unknown names become custom reaper entries. It replaces
the WARP watcher and sudoers rule; the old rule is moved into quietmac's
state folder. The old scripts stay in `~/.local/bin` but no longer run.

## Fresh Mac

Run steps 0 to 5. Skip `adopt`. On a brand-new Mac many daemons haven't
started yet, so `daemons.running` looks small. The reap modules still
apply. They act whenever the daemons start.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `this needs confirmation; re-run with --yes` | You're in a non-interactive shell. Ask the user, then pass `--yes`. |
| `the administrator password dialog was cancelled` | The user clicked Cancel; nothing changed. Ask again or skip the module. |
| `apply` exits with status 1 | At least one module wasn't done: not applicable here, refused, or stopped partway. The others still ran. A module that stopped partway shows `partial`, and `quietmac revert <id>` cleans it up. |
| A module stays `partial` after apply | `quietmac plan <id>` and `quietmac status` show what's missing. The profile stays `partial` until the user clicks Install. |
| WARP won't connect | `warp status`; `warp on` brings it up by hand. `quietmac status` flags a disabled daemon with no watcher. |
| The dev volume isn't mounted after a reboot | `quietmac devvol mount`, then check `/etc/fstab` with `quietmac devvol status`. See docs/DEV-VOLUME.md for manual recovery. |
| A migration was interrupted | `quietmac migrate status`. `migrate run <folder>` resumes; `migrate rollback <folder>` undoes. |
