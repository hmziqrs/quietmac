# Security

quietmac asks for your administrator password for some modules and runs root
scripts on your Mac, so security reports get priority.

## Reporting a problem

Please report vulnerabilities privately through GitHub: open the repository's
Security tab and choose "Report a vulnerability". Don't open a public issue
for anything that could give someone root or let them change what quietmac
runs as root. You should hear back within a week.

## What quietmac does with elevated rights

- Root steps are `/bin/sh` scripts. `quietmac plan <id>` prints them in full,
  and every one that runs is recorded in `~/Library/Logs/quietmac/actions.log`.
- In a terminal they run through `sudo`. Without one they go through the
  standard macOS administrator dialog (`osascript ... with administrator
  privileges`).
- `warp-on-demand` installs `/etc/sudoers.d/quietmac-warp`. It allows your
  user to run exactly four `launchctl` command lines for Cloudflare WARP's
  daemon without a password, and nothing else. The root script writes the
  rule itself into a root-only temporary file and validates it with `visudo`
  before installing it, so no user-writable file is ever installed as
  root.
- The reaper runs as you, not as root. It only signals processes owned by
  your user, and it refuses a built-in list of processes the session needs.

## Supported versions

Security fixes go into the latest release.
