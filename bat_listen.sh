#!/bin/bash
# bat_listen.sh -- MOUND bat listener (runs on yesteryearforever)
#
# Records a batch of short ultrasonic clips from the PDM mic, runs BatDetect2
# over the batch, and decides which clips hold a real bat pass:
#   a clip counts as a pass when it has at least PASS_MIN calls scoring
#   PASS_PROB or higher. (Tuned on the first nights of Oct 2026: this caught
#   10 of 12 real passes and zero katydids, crickets, clicks or hiss.)
# Passes get one line in passes.txt, their calls go in detections.txt,
# and a trimmed WAV + spectrogram of just the pass is saved to keep/.
#
# Run as root (needs real-time priority + raw I2S access):
#   sudo /home/ramblinray/bats/bat_listen.sh

set -u

USER_NAME=ramblinray
BASE=/home/$USER_NAME/bats
AUDIO=$BASE/audio
RESULTS=$BASE/results
KEEP=$BASE/keep
LOG=$BASE/detections.txt
PASSES=$BASE/passes.txt
ERR=$BASE/errors.txt
BATDETECT=/home/$USER_NAME/.local/bin/batdetect2

CLIPS=6           # clips per run (6 x 20s = 2 minutes of listening)
CLIP_SECS=20      # BatDetect2 wants clips under 30s
RATE=146739       # Pi 4 PDM clock (2347826 Hz) / 16
THRESHOLD=0.3     # BatDetect2's own cutoff (keep low; the pass rule decides)
PASS_PROB=0.5     # a call this confident counts toward a pass
PASS_MIN=2        # this many confident calls in one clip = a bat pass
KEEP_DAYS=30      # delete kept WAVs after this many days (PNGs + logs stay)

# -- Never let two runs overlap --------------------------
exec 9>/tmp/bat_listen.lock
flock -n 9 || exit 0

mkdir -p "$AUDIO" "$RESULTS" "$KEEP"
rm -f "$AUDIO"/* "$RESULTS"/*

[ -f "$LOG" ]    || echo "# timestamp, species_guess, det_prob, class_prob, low_khz, high_khz" > "$LOG"
[ -f "$PASSES" ] || echo "# timestamp, calls, best_prob, species_guess, low_khz, high_khz" > "$PASSES"

# -- Record --------------------------------------------
pdm_start 2.4 >/dev/null 2>&1

for i in $(seq 1 $CLIPS); do
  stamp=$(date +%Y-%m-%d_%H-%M-%S)
  chrt -f 99 pdm_rec $CLIP_SECS ch1 > "$AUDIO/$stamp.dat" 2>/dev/null
  sox -t raw -c 1 -r $RATE -e s -b 16 "$AUDIO/$stamp.dat" -b 16 "$AUDIO/$stamp.wav" 2>>"$ERR"
  rm -f "$AUDIO/$stamp.dat"
done

chown -R $USER_NAME:$USER_NAME "$BASE"

# -- Detect (as the normal user, where batdetect2 is installed)
sudo -H -u $USER_NAME "$BATDETECT" detect "$AUDIO/" "$RESULTS/" $THRESHOLD --quiet >/dev/null 2>>"$ERR"

# -- Decide which clips are real bat passes ------------
python3 - "$RESULTS" "$AUDIO" "$KEEP" "$LOG" "$PASSES" "$ERR" "$PASS_PROB" "$PASS_MIN" <<'EOF'
import json, sys, os, glob, subprocess
from collections import Counter
from datetime import datetime, timedelta

results, audio, keep, log, passes, err, pprob, pmin = sys.argv[1:9]
pprob, pmin = float(pprob), int(pmin)

for path in sorted(glob.glob(os.path.join(results, '*.json'))):
    try:
        with open(path) as f:
            data = json.load(f)
        calls = [a for a in data.get('annotation', [])
                 if float(a.get('det_prob', 0)) >= pprob]
        if len(calls) < pmin:
            continue                      # katydid, click, hiss... not a bat

        wav = data.get('id', os.path.basename(path).replace('.json', ''))
        stem = wav.replace('.wav', '')
        start = datetime.strptime(stem, '%Y-%m-%d_%H-%M-%S')

        # every confident call -> detections.txt
        with open(log, 'a') as out:
            for a in sorted(calls, key=lambda a: float(a.get('start_time', 0))):
                t = start + timedelta(seconds=float(a.get('start_time', 0)))
                out.write(
                    f"{t:%Y-%m-%d %H:%M:%S}, {a.get('class', '?')}, "
                    f"{float(a.get('det_prob', 0)):.2f}, {float(a.get('class_prob', 0)):.2f}, "
                    f"{float(a.get('low_freq', 0)) / 1000:.1f}, {float(a.get('high_freq', 0)) / 1000:.1f}\n")

        # one line per pass -> passes.txt
        t0 = min(float(a.get('start_time', 0)) for a in calls)
        t1 = max(float(a.get('end_time', a.get('start_time', 0))) for a in calls)
        best = max(float(a.get('det_prob', 0)) for a in calls)
        species = Counter(a.get('class', '?') for a in calls).most_common(1)[0][0]
        lo = min(float(a.get('low_freq', 0)) for a in calls) / 1000
        hi = max(float(a.get('high_freq', 0)) for a in calls) / 1000
        with open(passes, 'a') as out:
            out.write(f"{start + timedelta(seconds=t0):%Y-%m-%d %H:%M:%S}, {len(calls)}, "
                      f"{best:.2f}, {species}, {lo:.1f}, {hi:.1f}\n")

        # keep just the pass (+1 s either side) and draw its spectrogram
        src = os.path.join(audio, wav)
        snip = os.path.join(keep, stem + '.wav')
        a0 = max(0.0, t0 - 1.0)
        subprocess.run(['sox', src, snip, 'trim', f'{a0:.2f}', f'{(t1 + 1.0) - a0:.2f}'], check=True)
        subprocess.run(['sox', snip, '-n', 'highpass', '10k', 'norm', '-1',
                        'spectrogram', '-z', '60', '-x', '1200', '-y', '400',
                        '-t', stem, '-o', os.path.join(keep, stem + '.png')], check=True)
    except Exception as e:
        with open(err, 'a') as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}, ERROR on {path}: {e}\n")
EOF

# -- Housekeeping: old WAVs go, pictures and logs stay forever
find "$KEEP" -name '*.wav' -mtime +$KEEP_DAYS -delete

chown -R $USER_NAME:$USER_NAME "$BASE"
sudo -u ramblinray python3 /home/ramblinray/bats/publish_bats.py >> /home/ramblinray/bats/cron.log 2>&1
