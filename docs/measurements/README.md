# Measurements

Raw data behind the numbers in the docs, for anyone who wants to check them or
chart them. Everything was measured on one machine on 2026-11-01 unless the file
says otherwise: a MacBook Pro 14" with an M3 Max (Mac15,8, 16 cores), macOS 27.0
(26A428), SIP on. Its battery was designed for 72.4 Wh and holds about 63 Wh now
(5,458 of 6,249 mAh, 215 cycles).

| File | What's in it |
|---|---|
| [battery-by-day.csv](battery-by-day.csv) | Charge used per day while awake and asleep on battery, Oct 25 to Nov 1, from `quietmac battery` (macOS's power log) |
| [battery-sessions.csv](battery-sessions.csv) | The same week split into unplugged sessions of 20 minutes or more |
| [cpu-before-after.csv](cpu-before-after.csv) | CPU use of each process or group before and after the change that targets it |
| [reaper-cost.csv](reaper-cost.csv) | What the reaper itself costs, across the versions it went through |
| [power-calibration.csv](power-calibration.csv) | System draw at idle and with one busy core, used to turn CPU time into watts |

How the numbers were taken:

- CPU percentages are "% of one core": 100 means one core fully busy, so the
  whole 16-core chip is 1600. They come from `top` (which also reports
  Apple's energy-impact column without root) or from `ps` CPU time divided by
  wall time.
- Battery rates come from the charge level macOS logs at every sleep, wake
  and power-assertion change (`pmset -g log`). Readings are whole percents,
  so short sessions are noisy; daily totals are reliable. Watts assume the
  63 Wh capacity.
- Power calibration reads the battery's own current sensor (`ioreg
  AppleSmartBattery`) while unplugged, since `powermetrics` needs root.
- Day-to-day battery drain is dominated by what was running (browser, builds,
  coding agents), so the battery files describe the machine's real use. They
  aren't a controlled before-and-after of quietmac.

To produce the battery files on your own Mac: `./bin/quietmac battery --json`.
