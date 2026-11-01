# Contributing

Bug reports and pull requests are welcome. quietmac changes system settings and
runs root scripts, so the bar for changes is evidence: say what you measured,
on which Mac and macOS version.

## Setup

You need the Xcode command line tools (`xcode-select --install`) and nothing
else.

```sh
git clone https://github.com/hmziqrs/quietmac.git && cd quietmac
/usr/bin/python3 -m unittest discover -s tests -v
```

Unit tests never touch your system. `tests/integration.py` does real work, but
only inside a throwaway disk image and a temporary LaunchAgent, and it cleans
up after itself. Run it if you touch the dev volume, migration, launchd or
root-script code.

To try a module without changing anything, use `./bin/quietmac plan <id>`. To
apply it for real in a scratch location, set `QUIETMAC_ROOT` to an empty
folder. See [AGENTS.md](AGENTS.md) for what the sandbox does and doesn't do.

## Adding a daemon to a reap module

1. Run `./bin/quietmac probe <name> --seconds 90` and note the verdict.
2. Measure what it costs while alive (`./bin/quietmac measure`, or `top`).
3. Put the numbers in the pull request and, if they're new, in
   `docs/measurements/`.

Daemons that respawn in a loop, like chronod or the Siri AI app, belong on the
never-kill list instead.

## Style

- Python 3.9, standard library only.
- Every system change is a `core.Step`, and every apply records what revert
  needs.
- Docs: plain language, sentence-case headings, no em or en dashes. After
  editing docs, run `/usr/bin/python3 scripts/build_llms.py`.
- Add a line to CHANGELOG.md under "Unreleased" for anything a user would
  notice.
