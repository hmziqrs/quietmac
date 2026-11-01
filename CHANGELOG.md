# Changelog

All notable changes to quietmac. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-01

First release.

### Added

- Modules, each with plan, apply and revert:
  - reap-appstore, reap-media-analysis, reap-siri-helpers, reap-shortcuts,
    reap-suggestions and reap-widgets
  - handoff-off, airdrop-off, universal-control-off and bluetooth-off
  - warp-on-demand
  - spotlight-off
  - low-power-on-battery, powernap-off-on-battery and
    display-sleep-on-battery
  - privacy-profile
  - dev-volume
  - two guided items: nc-widgets-remove and activity-monitor-habit
- The daemon reaper, a LaunchAgent that kills unwanted per-user Apple
  daemons. It reads the process list through libproc, filtered to your user
  by the kernel. It has a never-kill list and a two-stage churn guard: watch
  mode first, giving up only after that.
- `quietmac detect`, `list`, `questions`, `setup`, `status`, `doctor`,
  `measure`, `battery`, `probe` and `adopt`.
- A dev volume, with safe folder migration (copy, verify, switch, roll back)
  and cache relocation for bun, pnpm, the Go build cache, Gradle, Xcode
  DerivedData and npm.
- INSTALL.md, a step-by-step install script for coding agents, plus
  AGENTS.md, llms.txt and llms-full.txt.
- Unit tests, an opt-in integration suite, and CI on macOS 15 and macOS 26.

[Unreleased]: https://github.com/hmziqrs/quietmac/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hmziqrs/quietmac/releases/tag/v0.1.0
