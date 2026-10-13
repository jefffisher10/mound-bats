# mound-bats

An automated ultrasonic bat listener, part of **MOUND** (Monitoring Outdoor Urban Naturalist Device), a backyard sensor station in Canal Winchester, Ohio that publishes to [yesteryearforever.xyz](https://yesteryearforever.xyz).

Every night a microphone under the garage eave listens for echolocation calls, a neural network picks out the bats, and anything real gets logged, trimmed, drawn as a spectrogram, and backed up here.

First bat heard: **October 6, 2026 at 19:40.** It came back at 19:40 the next night, too.

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 4 ("yesteryearforever") | AC powered; also runs the website |
| SPH0641LU4H-1 PDM MEMS microphone | Cheap, tiny, hears well past 20 kHz |
| Cat6 cable | Twisted pairs (CLK+GND, DATA+GND) killed the static |

Wiring to the Pi's I2S pins:

| Mic | Pi |
|---|---|
| VDD | 3.3V |
| GND | GND |
| CLK | GPIO18 (pin 12) |
| DATA | GPIO20 (pin 38) |
| SEL | GND |

`/boot/firmware/config.txt` (or `/boot/config.txt` on older Pi OS) needs `dtparam=i2s=on`.

## Software

- **[pdm_rec](https://github.com/assi-dangomushi/pdm_rec)**: raw PDM capture. On a Pi 4, `pdm_start 2.4` gives a 2,347,826 Hz clock, which works out to a **146,739 Hz** sample rate.
- **sox**: converts raw PDM to WAV, trims clips, draws spectrograms.
- **[BatDetect2](https://github.com/macaodha/batdetect2)**: neural network bat call detector. Needs the CPU-only PyTorch build on a Pi 4 (the CUDA wheels crash with "Illegal instruction").

## How it works

`bat_listen.sh` runs from root's crontab every 10 minutes, 18:00 to 06:50:

```
*/10 18-23,0-6 * * * /home/ramblinray/bats/bat_listen.sh >> /home/ramblinray/bats/cron.log 2>&1
```

Each run:

1. Records six 20-second clips (2 minutes of listening out of every 10).
2. Runs BatDetect2 over them at a low threshold (0.3).
3. Applies the **pass rule**: a clip counts as a bat pass only if it has **at least 2 calls scoring 0.5 or higher**.
4. For each pass: logs it, trims the WAV to the calls plus 1 second either side, and draws a spectrogram.
5. Deletes kept WAVs older than 30 days. Spectrograms and logs stay forever.

A separate job in ramblinray's crontab commits and pushes this repo every morning at 07:05.

### Why the pass rule?

BatDetect2 alone flagged katydids, crickets, dew drips and plain mic hiss as "bats," sometimes scoring them as high as 0.64. But real bats call 5 to 10 times a second, so they always produce **bursts** of confident detections, while noise produces lone hits.

Tested against hand-checked spectrograms from the first three nights (187 clips):

| | Clips | Flagged by the rule |
|---|---|---|
| Real bat passes | 12 | 10 |
| Katydids, crickets, clicks, hiss | 175 | **0** |

The two misses were faint, distant passes.

## Files

| File | What's in it |
|---|---|
| `bat_listen.sh` | The whole listener |
| `passes.txt` | One line per bat visit: `timestamp, calls, best_prob, species_guess, low_khz, high_khz` |
| `detections.txt` | Every confident call from every pass |
| `keep/` | Spectrogram PNGs (and trimmed WAVs, for 30 days) |
| `first_nights/` | The first 12 bat recordings, Oct 6 to 8, 2026, full 20-second clips, kept forever |
| `detections_first_nights.txt` | The raw, unfiltered log from before the pass rule existed |
| `errors.txt` | Anything that went wrong |

## Who's out there?

BatDetect2 was trained on **UK bats**, so its species labels are "nearest British equivalent," not real IDs. Going by call frequency and shape instead:

- **~25 to 30 kHz, shallow sweeps:** almost certainly a **big brown bat** (or possibly a silver-haired bat). Labeled "Nyctalus" or "Eptesicus" in the logs. This is the 19:40 commuter.
- **~38 to 50 kHz, steep hooked sweeps:** a second species, first heard Oct 9. Best guess is an **eastern red bat** migrating through in fall; possibly a Myotis. Labeled "Pipistrellus" in the logs.

## Known limitations

- The PDM mic's noise floor rises above ~30 kHz and swamps everything above ~50 kHz, so the tops of high calls are lost.
- Only listening 20% of the time, so timestamps are ±10 minutes and some passes are missed.
- The mic is under an eave pointing down, near shrubs full of katydids. Not ideal.
- Ohio bats go quiet from roughly late October to April.

## Ideas

- Bat activity graph on the website, under the MOUND weather graph
- "Last night's best pass" spectrogram on the site
- Start listening relative to sunset instead of a fixed 18:00
- A better ultrasonic mic in the spring

---

Built by Jeffrey with Claude, October 2026.
