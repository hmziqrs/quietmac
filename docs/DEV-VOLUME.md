# The dev volume

## Why

With many coding agents and builds running in parallel, files churn
constantly: `node_modules`, `target/`, `build/`, `.dart_tool`, git objects,
lock files. Every change goes through **fseventsd**, which also writes a
persistent per-volume history of events, and through **Spotlight**, which
indexes the results. People running many agents report fseventsd climbing
after a few days. Restarting fseventsd on a schedule is the wrong fix: it
runs as root, and every watcher and Spotlight lose their history and
rescan.

A separate APFS volume lets you switch both off **for code only**:

- `.fseventsd/no_log` at the volume root: fseventsd keeps no persistent
  history for it. Live file watching still works: on macOS 27, Node's
  `fs.watch` (which uses FSEvents) got 41 events on a no_log volume and 42
  on a normal one. Dev servers, editors and watchman keep working.
- Spotlight indexing off on that volume only, so Spotlight keeps working
  everywhere else.

**What it saves, measured:** no fseventsd CPU. On macOS 27 (M3 Max,
2026-11-02), the same clean `cargo build` (175 crates, about 2,000 files in
`target/`, 12.5 s) ran three times on the Data volume and three times on a
no_log dev volume, alternating, with idle windows in between to subtract
the background. Each build produced about 4,000 FSEvents on both volumes,
and fseventsd spent about 0.9 s of CPU per build on both (0.84 to 0.95 s on
Data, 0.95 to 1.08 s on the dev volume). fseventsd's work is taking events
from the kernel and delivering them to watchers, and no_log doesn't change
that. The log it skips is small: no build added more than two log files to
the Data volume's `.fseventsd`.

So the volume is worth it only for Spotlight: it keeps code out of the
index while Spotlight stays on for everything else. If Spotlight is already
off (`quietmac list` shows `spotlight-off`), skip it.

**Zero events instead of no log.** fseventsd also honors an undocumented
`.fseventsd/ignore` file (found in its binary on macOS 27). It only checks
for it when `no_log` is absent. With `ignore`, fseventsd logs "will be
completely ignored" and the volume delivers no FSEvents at all: the same
clean build cost fseventsd nothing above its idle rate, against about 1 s
of CPU on the Data volume. The price is that folder watchers on that volume
see nothing: Node's `fs.watch`, chokidar, Vite and Next hot reload, Rust's
`notify` (cargo watch, bacon), Python's watchdog and watchfiles, and
editors. Watches on single files (kqueue, which Go's fsnotify uses) still
work. It still doesn't buy battery life: with about ten agents running,
fseventsd averaged around 1% of one core on the test Mac. quietmac doesn't
set `ignore`.

**macOS 27 blocks `/etc/fstab`.** The system policy denies creating it, even
as root (`(Sandbox) System Policy: touch deny(1) file-write-create
/private/etc/fstab`). `devvol create` tries fstab first and stops before
creating anything. A dev volume set up by hand on macOS 27 has no fstab
line, so nothing mounts it at your folder after a reboot.

## What `quietmac devvol create` does

One root script (one password prompt), printed in full by
`quietmac plan dev-volume`:

1. `diskutil apfs addVolume <startup container> APFS Dev -nomount`. The volume
   shares free space with the rest of the disk: no fixed size, nothing
   reserved.
2. Creates the mount folder (default `~/dev`), owned by `root:wheel`, mode
   `000`. While the volume isn't mounted, writes there fail instead of
   silently landing on the main disk.
3. Backs up `/etc/fstab` to `/etc/fstab.quietmac-backup`, then appends:
   ```
   # quietmac dev volume
   UUID=<volume uuid> /Users/<you>/dev apfs rw,nobrowse,owners
   ```
   `nobrowse` keeps it off the Desktop and Finder sidebar (it's just a folder
   in your home); `owners` means real permissions.
4. Mounts it, makes you the owner, creates `.fseventsd/no_log` and
   `.metadata_never_index`, runs `mdutil -i off`, then remounts, so fseventsd
   sees `no_log` from the start.

At boot, macOS mounts it from `/etc/fstab` by UUID.

**Encryption:** the volume isn't protected by your login password; FileVault
covers the main volume only. On Apple silicon the SSD still encrypts
everything in hardware, tied to this Mac. In practice, what's exposed if the
laptop is stolen is what's on this volume: mostly `.env` files with API
keys. Rotate those if that ever happens. Your keychain, SSH keys and
browser data stay on the FileVault volume.

**Time Machine:** if you use it, check whether you want the volume backed
up (System Settings > General > Time Machine > Options).

## Moving folders: `quietmac migrate`

```sh
cd ~
quietmac migrate plan ~/os ~/ps    # sizes, free space, anything busy inside
quietmac migrate run ~/os ~/ps
```

For each folder (e.g. `~/os` → `~/dev/os`):

1. **Preflight.** It checks that:
   - the volume is mounted
   - the folder is real (not a symlink)
   - it isn't a macOS-managed folder (Desktop, Documents, Library, ...)
   - there's room for a full copy, since the original stays as a backup
   - no running process has its working directory inside the folder.
     Shells, editors, dev servers and agents are listed, and quietmac
     refuses until they're closed.
2. **Copy** with `ditto` to `~/dev/os.quietmac-partial`. This keeps hard
   links, extended attributes, ACLs, permissions and times.
3. **Verify.** Every path, type, size, mode, mtime and symlink target must
   match; `--verify checksum` compares every byte. Any difference stops it,
   and the original isn't touched.
4. **Switch.** The copy is renamed into place, the original becomes
   `~/os.quietmac-backup-<date>`, and `~/os` becomes a symlink to
   `~/dev/os`. Old paths keep working.
5. **Claude Code.** Claude Code keeps per-project history and memory in
   `~/.claude/projects/<path with every non-alphanumeric character as ->`.
   Programs that ask the OS for their working directory see the physical
   path (`/Users/you/dev/os/...`), not the link. So quietmac adds symlinked
   aliases for the new paths that point at the existing folders, and history
   carries over. Skip this with `--no-claude-aliases`.

Each step is written to `~/.local/state/quietmac/state.json` before it
happens. An interrupted run is resumed by running the same command again,
or undone with `migrate rollback`.

- `quietmac migrate rollback ~/os` restores the original backup. It refuses
  if files changed on the volume since the move.
- `quietmac migrate rollback ~/os --keep-changes` copies the current files
  back and verifies them instead. The dev-volume copy is parked as
  `~/dev/os.rolledback-<date>`.
- `quietmac migrate cleanup` lists backups and parked copies with their sizes,
  asks, then deletes them.

If quietmac's own checkout is inside a folder being moved, it re-runs itself
from a temporary copy.

After moving JavaScript projects, `rm -rf node_modules && bun install` (or
pnpm) on the volume brings back clone/hard-link savings. Copied
`node_modules` are full copies.

## Caches: `quietmac cache`

```sh
quietmac cache list
quietmac cache move bun go-build
quietmac cache restore bun
```

Each moved cache is copied to `~/dev/.caches/<name>` (or emptied with
`--drop`, since caches rebuild themselves), and the old path becomes a
symlink.

| Cache | Path | Move it? |
|---|---|---|
| bun | `~/.bun/install/cache` | **Yes, if JS projects move.** bun clones from it, and clones only work within one volume. |
| pnpm | `pnpm store path` | **Yes, if JS projects move.** It hard-links from the store, and hard links only work within one volume. |
| go-build | `go env GOCACHE` | Optional: written on every Go build. |
| gradle | `~/.gradle/caches` | Optional: Android builds write here constantly. |
| xcode-derived-data | `~/Library/Developer/Xcode/DerivedData` | Optional: Xcode and Flutter iOS builds. |
| npm | `~/.npm/_cacache` | Small gain: only written on downloads. |

**Toolchains stay on the main volume:** rustup, cargo, Go modules, the
Flutter and Android SDKs, Homebrew, and global bun/npm tools. They're written
once when installed or updated, then only read. Reads cost fseventsd
nothing, and installers expect their default paths.

`quietmac doctor` warns if a tool replaced a cache symlink with a real
folder.

## Removing the volume

```sh
quietmac migrate rollback ~/os ~/ps   # or move things off by hand
quietmac cache restore bun
quietmac migrate cleanup
quietmac devvol destroy               # refuses while anything still lives there, or while it's unmounted
                                      # (it can't check it's empty); --force overrides both
```

## Manual recovery (if quietmac itself is unavailable)

```sh
diskutil apfs list                                   # find the volume ("Dev") and its UUID
sudo diskutil mount nobrowse -mountOptions owners -mountPoint ~/dev <UUID>   # mount it by hand
sudo vifs                                            # edit /etc/fstab; the line follows "# quietmac dev volume"
# undo one migration by hand: the original is untouched next to the link
rm ~/os && mv ~/os.quietmac-backup-<date> ~/os
sudo diskutil apfs deleteVolume <UUID>               # delete the volume (and everything on it)
```

## Prior art

- elithrar/dotfiles `repos-volume.sh`: the same idea, a single `~/repos`
  volume with `no_log` and Spotlight off, mounted from fstab.
- Discussions among people running many parallel agents on Macs, where
  fseventsd load grew over days.
