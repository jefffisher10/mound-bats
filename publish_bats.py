#!/usr/bin/env python3
# publish_bats.py -- turns the bat listener's logs into public web pages
#
# Runs on yesteryearforever (YYF) as ramblinray, at the end of bat_listen.sh.
# Reads ~/bats/passes.txt + ~/bats/keep/*.png and writes, under /var/www/html/bats/:
#   spectrograms/<stem>.png   copies of each pass's spectrogram (kept forever)
#   sounds/<stem>.mp3         each pass slowed 10x so humans can hear it (kept forever)
#   latest.json               the most recent pass, for the "last bat heard" block
#   index.html                the full archive, grouped by night
#
# Rebuilds everything from scratch each run -- no state to track. Files are only
# rewritten when their contents actually change, so git stays quiet.

import html
import json
import os
import shutil
import subprocess
from bisect import bisect_right
from collections import OrderedDict
from datetime import datetime, timedelta

BATS = os.environ.get('BATS_DIR', '/home/ramblinray/bats')
WWW = os.environ.get('BATS_WWW', '/var/www/html/bats')

PASSES = os.path.join(BATS, 'passes.txt')
KEEP = os.path.join(BATS, 'keep')
FIRST_NIGHTS = os.path.join(BATS, 'first_nights')
ERRORS = os.path.join(BATS, 'errors.txt')
SPEC_DIR = os.path.join(WWW, 'spectrograms')
SOUND_DIR = os.path.join(WWW, 'sounds')

# -- Making bats audible -------------------------------------
# Cut everything below 15 kHz (hum, hiss, the audible world), turn it up as far
# as it goes, then slow it 10x: a 27 kHz call becomes a 2.7 kHz chirp.
# 12,000 samples/s still holds anything up to 60 kHz in the original.
SLOW_EFFECTS = ['highpass', '15k', 'norm', '-1', 'speed', '0.1', 'rate', '12000']
# first_nights/ clips are the full 20 s; slow just this much from the first call
FIRST_NIGHT_SECS = 6

# A pass's timestamp is "clip start + offset of first call", and clips are 20 s,
# so its spectrogram is the newest keep/ file at or before the pass, within this window.
MATCH_WINDOW = 25  # seconds

# Passes before noon belong to the previous evening's night.
NIGHT_ROLLOVER_HOUR = 12


# -- Who was it? ---------------------------------------------
# BatDetect2 was trained on UK bats, so its labels are "nearest British
# equivalent." This maps each label to its closest Ohio counterpart by call
# shape and frequency. Shown small, as the detector's opinion.
UK_TO_OHIO = {
    'Eptesicus serotinus':       'big brown bat',
    'Nyctalus noctula':          'hoary bat',
    'Nyctalus leisleri':         'big brown or silver-haired bat',
    'Vespertilio murinus':       'silver-haired bat',
    'Pipistrellus nathusii':     'eastern red bat',
    'Pipistrellus pipistrellus': 'eastern red or tri-colored bat',
    'Pipistrellus pygmaeus':     'tri-colored bat or a Myotis',
    'Barbastellus barbastellus': 'a Myotis',
    'Myotis daubentonii':        'a Myotis (little brown?)',
    'Myotis mystacinus':         'a Myotis (little brown?)',
    'Myotis brandtii':           'a Myotis (little brown?)',
    'Myotis nattereri':          'a Myotis (northern long-eared?)',
    'Myotis bechsteinii':        'a Myotis (northern long-eared?)',
    'Myotis alcathoe':           'a Myotis',
    'Plecotus auritus':          'a quiet gleaning bat',
    'Plecotus austriacus':       'a quiet gleaning bat',
    'Rhinolophus ferrumequinum': 'unknown (no Ohio match)',
    'Rhinolophus hipposideros':  'unknown (no Ohio match)',
}

# The headline guess goes by the lowest frequency in the pass, which is what
# actually tells Ohio bats apart. (low_khz, label) -- first band that fits wins.
FREQ_BANDS = [
    (23, 'hoary bat?'),
    (32, 'big brown bat'),
    (45, 'eastern red bat?'),
    (999, 'a Myotis or tri-colored bat?'),
]


def guess_by_freq(low_khz):
    for limit, label in FREQ_BANDS:
        if low_khz < limit:
            return label
    return 'unknown'


def ohio_cousin(uk_label):
    return UK_TO_OHIO.get(uk_label, 'no Ohio match on file')


# -- Read the logs -------------------------------------------
def read_passes():
    passes = []
    try:
        with open(PASSES) as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 6:
                    continue
                try:
                    passes.append({
                        'time': datetime.strptime(parts[0], '%Y-%m-%d %H:%M:%S'),
                        'calls': int(parts[1]),
                        'best': float(parts[2]),
                        'detector': parts[3],
                        'low': float(parts[4]),
                        'high': float(parts[5]),
                    })
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    passes.sort(key=lambda p: p['time'])
    return passes


def read_spectrograms():
    """Sorted list of (clip start time, stem) for every PNG in keep/."""
    found = []
    try:
        for name in os.listdir(KEEP):
            if not name.endswith('.png'):
                continue
            stem = name[:-4]
            try:
                found.append((datetime.strptime(stem, '%Y-%m-%d_%H-%M-%S'), stem))
            except ValueError:
                continue
    except FileNotFoundError:
        pass
    found.sort()
    return found


def match_spectrogram(t, specs, spec_times):
    """Newest clip that started at or before t, within the match window."""
    i = bisect_right(spec_times, t) - 1
    if i >= 0 and (t - specs[i][0]).total_seconds() <= MATCH_WINDOW:
        return specs[i][1]
    return None


def night_of(t):
    return (t - timedelta(days=1)).date() if t.hour < NIGHT_ROLLOVER_HOUR else t.date()


# -- Write helpers (only touch files that changed) -----------
def write_if_changed(path, text):
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)  # atomic: Apache never serves half a file
    return True


def copy_if_changed(src, dst):
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return False
    shutil.copy2(src, dst + '.tmp')
    os.replace(dst + '.tmp', dst)
    return True


# -- Sounds --------------------------------------------------
def sound_format():
    """mp3 if this sox can write it (about 10x smaller), else plain wav."""
    try:
        out = subprocess.run(['sox', '-h'], capture_output=True, text=True).stdout
        formats = out.split('AUDIO FILE FORMATS:')[1].split('\n')[0].split()
        return 'mp3' if 'mp3' in formats else 'wav'
    except Exception:
        return 'wav'


def log_error(msg):
    with open(ERRORS, 'a') as f:
        f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S}, publish_bats: {msg}\n')


def make_sound(p, fmt):
    """Slow a pass down 10x into sounds/. Returns the file name, or None."""
    stem = p['image']
    if not stem:
        return None
    for ext in ('mp3', 'wav'):                      # already made (in either format)
        if os.path.exists(os.path.join(SOUND_DIR, f'{stem}.{ext}')):
            return f'{stem}.{ext}'

    kept = os.path.join(KEEP, stem + '.wav')         # already trimmed to just the pass
    first = os.path.join(FIRST_NIGHTS, stem + '.wav')  # full clip: trim it here
    if os.path.exists(kept):
        src, trim = kept, []
    elif os.path.exists(first):
        clip_start = datetime.strptime(stem, '%Y-%m-%d_%H-%M-%S')
        a0 = max(0.0, (p['time'] - clip_start).total_seconds() - 1.0)
        src, trim = first, ['trim', f'{a0:.2f}', str(FIRST_NIGHT_SECS)]
    else:
        return None                                  # WAV aged out of keep/ before we got to it

    name = f'{stem}.{fmt}'
    out = os.path.join(SOUND_DIR, name)
    tmp = os.path.join(SOUND_DIR, f'{stem}.tmp.{fmt}')
    opts = ['-C', '32'] if fmt == 'mp3' else []
    r = subprocess.run(['sox', src] + opts + ['-c', '1', tmp] + trim + SLOW_EFFECTS,
                       capture_output=True, text=True)
    if r.returncode != 0:
        log_error(f'sound failed for {stem}: {r.stderr.strip()[:200]}')
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    os.replace(tmp, out)
    return name


# -- Page pieces ---------------------------------------------
NAV = """  <nav id="topnav">
    <div id="topnav-inner">
      <a href="../ABOUT.html">| about |</a>
      <span class="nav-sep">·</span>
      <a href="../making.html">| making |</a>
      <span class="nav-sep">·</span>
      <a href="../writing.html">| writing |</a>
      <span class="nav-sep">·</span>
      <a href="../sensing.html">| sensing |</a>
      <span class="nav-sep">·</span>
      <button id="toggle-dark-mode">| dark mode |</button>
      <span class="nav-sep">·</span>
      <button onclick="goRandom()">| random |</button>
    </div>
  </nav>"""

HR = '<hr style="border:none; border-top: 1px solid #ccc; margin: 1.5rem 0;">'


def pass_html(p):
    e = html.escape
    when = p['time'].strftime('%H:%M')
    lines = [
        '      <div style="margin: 0 0 1.5rem 0;">',
        f'        <p style="margin:0 0 0.4rem 0;"><strong>{when}</strong>'
        f' &nbsp;&middot;&nbsp; {e(p["guess"])}'
        f' &nbsp;&middot;&nbsp; {p["calls"]} call{"s" if p["calls"] != 1 else ""}'
        f' &nbsp;&middot;&nbsp; {p["low"]:.0f}&ndash;{p["high"]:.0f} kHz<br>',
        f'        <span style="font-size:0.85rem; color:#999;">detector: {e(p["detector"])}'
        f' (UK) &asymp; {e(p["cousin"])} &nbsp;&middot;&nbsp; confidence {p["best"]:.2f}</span></p>',
    ]
    if p['image']:
        lines.append(
            f'        <img src="spectrograms/{p["image"]}.png" data-full="spectrograms/{p["image"]}.png"'
            f' alt="Spectrogram of a bat pass at {when}" class="thumbnail" loading="lazy"'
            f' style="width:100%; max-width:600px; height:auto; display:block; cursor:zoom-in;">')
    if p.get('sound'):
        mime = 'audio/mpeg' if p['sound'].endswith('.mp3') else 'audio/wav'
        lines.append(
            f'        <audio controls preload="none" style="width:100%; max-width:600px; display:block; margin-top:0.4rem;">'
            f'<source src="sounds/{p["sound"]}" type="{mime}"></audio>')
    lines.append('      </div>')
    return '\n'.join(lines)


def build_index(passes):
    nights = OrderedDict()
    for p in sorted(passes, key=lambda p: p['time'], reverse=True):
        nights.setdefault(night_of(p['time']), []).append(p)

    if passes:
        first = passes[0]['time']
        summary = (
            f'      <p>{len(passes)} bat pass{"es" if len(passes) != 1 else ""} on '
            f'{len(nights)} night{"s" if len(nights) != 1 else ""}, '
            f'first heard {first:%B} {first.day}, {first.year}.<br>\n'
            '      <span style="font-size:0.85rem; color:#999;">Times are when the first call was heard. '
            'Nights run from evening to the next morning.</span></p>'
        )
    else:
        summary = '      <p>No bats yet. Listening every night from dusk to dawn.</p>'

    sections = []
    for night, night_passes in nights.items():
        n = len(night_passes)
        sections.append(
            f'      {HR}\n'
            f'      <p><strong>{night:%A, %B} {night.day}, {night.year}</strong>'
            f' &nbsp;<span style="color:#999;">({n} pass{"es" if n != 1 else ""})</span></p>\n'
            + '\n'.join(pass_html(p) for p in night_passes)
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MOUND -- Bat Archive | yesteryear forever</title>
  <link rel="stylesheet" href="../style.css" />
  <script src="../script.js" defer></script>
</head>
<body>

{NAV}

  <div class="container">
    <h1><a href="../index.html">| yesteryear forever |</a></h1>

    <blockquote>

      <p><strong>MOUND -- Every Bat It's Heard</strong></p>

      <p><a href="../mound/data_live.html">&larr; live data</a> &nbsp;&middot;&nbsp;
      <a href="../sensing.html">about this project</a></p>

{summary}

      <p style="font-size:0.85rem; color:#999;">Every recording is slowed down ten times, so calls too
      high for human ears drop into hearing range. Headphones help.</p>

      <p style="font-size:0.85rem; color:#999;">Species are educated guesses from call frequency.
      The detector was trained on British bats, so its labels are shown with their closest
      Ohio cousin. Treat all of it as a good story, not a field guide.</p>

{chr(10).join(sections)}

    </blockquote>

    <p style="font-size: 0.85rem; color: #999;">auto-generated</p>

  </div>

  <div class="lightbox" id="lightbox">
    <img id="lightbox-img" src="" alt="">
  </div>

</body>
</html>
"""


def build_latest(passes):
    if not passes:
        return {'heard': False}
    p = passes[-1]
    return {
        'heard': True,
        'time': p['time'].strftime('%Y-%m-%d %H:%M:%S'),
        'calls': p['calls'],
        'best': p['best'],
        'low_khz': p['low'],
        'high_khz': p['high'],
        'guess': p['guess'],
        'detector': p['detector'],
        'detector_ohio': p['cousin'],
        'image': f'/bats/spectrograms/{p["image"]}.png' if p['image'] else None,
        'sound': f'/bats/sounds/{p["sound"]}' if p.get('sound') else None,
        'total_passes': len(passes),
        'total_nights': len({night_of(q['time']) for q in passes}),
    }


# -- Main ----------------------------------------------------
def main():
    os.makedirs(SPEC_DIR, exist_ok=True)
    os.makedirs(SOUND_DIR, exist_ok=True)

    passes = read_passes()
    specs = read_spectrograms()
    spec_times = [t for t, _ in specs]
    fmt = sound_format()
    sounds_before = len(os.listdir(SOUND_DIR))

    copied = 0
    for p in passes:
        p['guess'] = guess_by_freq(p['low'])
        p['cousin'] = ohio_cousin(p['detector'])
        p['image'] = match_spectrogram(p['time'], specs, spec_times)
        if p['image']:
            src = os.path.join(KEEP, p['image'] + '.png')
            dst = os.path.join(SPEC_DIR, p['image'] + '.png')
            if copy_if_changed(src, dst):
                copied += 1
        p['sound'] = make_sound(p, fmt)

    write_if_changed(os.path.join(WWW, 'latest.json'),
                     json.dumps(build_latest(passes), indent=1) + '\n')
    write_if_changed(os.path.join(WWW, 'index.html'), build_index(passes))

    unmatched = sum(1 for p in passes if not p['image'])
    silent = sum(1 for p in passes if not p['sound'])
    made = len(os.listdir(SOUND_DIR)) - sounds_before
    print(f'{datetime.now():%Y-%m-%d %H:%M:%S} publish_bats: {len(passes)} passes, '
          f'{copied} new spectrograms, {made} new sounds ({fmt}), '
          f'{unmatched} without a picture, {silent} without a sound')


if __name__ == '__main__':
    main()
