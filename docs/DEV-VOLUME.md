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

**When it's worth it:** run `quietmac measure`. If fseventsd's average is
well above ~2% of a core after a few days of normal work, do it. Below that
(the test Mac was at 0.48% after 5 hours), it saves very little.

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

- **`quietmac migrate rollback ~/os`** restores the original backup. It
  refuses if files changed on the volume since the move.
- **`quietmac migrate rollback ~/os --keep-changes`** copies the current files
  back and verifies them instead. The dev-volume copy is parked as
  `~/dev/os.rolledback-<date>`.
- **`quietmac migrate cleanup`** deletes backups and parked copies, after
  listing them with sizes and asking.

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
quietmac devvol destroy               # refuses while anything still lives there; --force deletes leftovers
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
