"""Govee H706C (Permanent Lights Pro) realtime engine - per-LED animations over the LAN, driven from Home Assistant.

Runs on an always-on PC on the same network as the lights. Paints frames over the LAN realtime ('razer') protocol and
follows Home Assistant (see ha/govee_package.yaml):
  input_select.govee_rt_scene : Off | Hyperspace | Imperial March | Saber Duel | Duel + March | LED Sweep | Halloween Glow
                                | Custom (the theme builder's last design) | <saved design>  (the engine adds saved designs itself)
  input_text.govee_rt_cmd     : commands from govee-rt.html, 'v1|<mode>|<N>|<rrggbb>|<arg>|#nonce'
        fill | alt | grad | chase (arg = s/step) | mask (arg = hex bitmask) | bri (arg = %: the strip's own brightness) | off
        set (arg = name=value, tunes an effect knob live, saved in params.json) | reset (arg = effect: its knobs to defaults)
        preview (arg = effect | Off) | pset (arg = name=value, preview only) | preset (arg = effect) | apply (arg = effect:
        copy that effect's preview knobs to the live ones)
  sensor.govee_rt_design      : theme builder messages (a design is too big for an input_text), attributes
        {to: preview | roof | save | delete | diy | diydel, spec: {layers: [...]}, name, fx: {effect, speed, pct}, apply, nonce}
Whichever changed last wins. Off = realtime exit packet, strip brightness back to roof_bri, then script.govee_apply_theme
re-applies the theme in sensor.govee_applied_theme (HA decides LAN DIY vs cloud scene).
Status -> sensor.govee_rt_engine (state = mode; attrs n, cmd, led, frames, fps, since, params, preview_*, design, designs,
diy, zones, gap_max_ms). Live view -> <www_dir>/govee_frame.json, preview -> <www_dir>/govee_preview.json: the LED colours
as hex plus 'hist', the last 1.2 s of frames, which the page replays 0.5 s behind on its own clock (writes to a network
share land unevenly and would otherwise read as stutter).

Protocol facts (measured on an H706C):
  * Frames: BB 00 <len> B0 00 <n> RGB*n <xor>, at most max_pixels per frame - a bigger frame is silently dropped and the
    strip holds its last good one. Nothing shows until the enable packet (BB 00 01 B1 01 + xor = 'uwABsQEK') is sent;
    re-sending it while running flashes the strip black, so enable_every defaults to 0 (only at start).
  * Pixel -> LED mapping follows the app's "number of segments" settings: with the strip "cut" at its real LED count,
    max_pixels = leds and pixel = LED; with a longer nominal length the strip takes fewer pixels and lights LEDs in pairs.
  * The strip's own brightness (LAN 'brightness') dims realtime frames too, with finer steps than scaling the colours:
    gain is applied as strip brightness roof_bri * gain and frames go out at full range.

    python govee_rt_engine.py                    # service (what the watchdog starts)
    python govee_rt_engine.py <Mode> [secs]      # one-shot effect on the real lights, then restore
    python govee_rt_engine.py --simulate [secs]  # render every effect, builder motion and controller preview offline: no UDP, no HA

LED zones come from config.json ('zones'); every zone is optional and effects fall back to the whole strip.
`pip install websocket-client` lets the engine add saved designs to the input_select; everything else is the standard library.
"""
import sys, os, re, json, time, math, random, socket, base64, datetime, threading, urllib.request
if os.name == 'nt':
    try: import ctypes; ctypes.windll.winmm.timeBeginPeriod(1)   # 1 ms sleeps: Windows' default 15.6 ms ran "20 fps" at an uneven 16
    except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg_path = os.environ.get('GOVEE_CONFIG') or os.path.join(HERE, 'config.json')
if not os.path.exists(_cfg_path): _cfg_path = os.path.join(HERE, 'example_config.json')
CFG = json.load(open(_cfg_path))
IP = CFG['govee_ip']
SUP = os.environ.get('SUPERVISOR_TOKEN')                        # set inside the Home Assistant add-on: Core is reached through the Supervisor proxy
HA = 'http://supervisor/core' if SUP else CFG['ha_url'].rstrip('/')
WS_URL = 'ws://supervisor/core/websocket' if SUP else HA.replace('http', 'ws', 1) + '/api/websocket'
SEL, CMD, STATUS, DESIGN = 'input_select.govee_rt_scene', 'input_text.govee_rt_cmd', 'sensor.govee_rt_engine', 'sensor.govee_rt_design'
LIGHT, APPLIED, APPLY_SCRIPT = CFG.get('light_entity', 'light.permanent_lights_pro'), 'sensor.govee_applied_theme', 'script.govee_apply_theme'
DEFAULT_THEME = CFG.get('default_theme')                        # restored on stop when the applied theme is itself 'RT: ...'
N = int(CFG.get('leds', 80))                                    # physical LEDs the effects draw on
N_DEV = int(CFG.get('max_pixels', N))                           # max pixels per frame the strip accepts
RAZER_CTRL = os.environ.get('GOVEE_RAZER_CTRL', '1') == '1'     # enable packet before frames (required) + exit packet on stop
WWW = os.environ.get('GOVEE_WWW') or CFG.get('www_dir', HERE)
DATA = os.environ.get('GOVEE_DATA', HERE)                       # knobs + saved designs (the add-on keeps them next to config.json)
PARAMS_PATH, DESIGNS_PATH = os.path.join(DATA, 'params.json'), os.path.join(DATA, 'designs.json')
FRAME_PATH, PREVIEW_PATH, DIY_PATH = [os.path.join(WWW, f) for f in ('govee_frame.json', 'govee_preview.json', 'govee_diy_themes.json')]
DEFAULTS = {'fps': 20, 'gain': 1.0,
            'enable_every': 0.0,   # re-send the realtime enable packet every N s while running (0 = only at start; re-sending flashed the strip black)
            'roof_bri': 40,        # the strip's own brightness % outside realtime (= your app setting); while running it is set to roof_bri * gain
            'duel_speed': 12.0, 'duel_amp': 2.0, 'duel_freq': 1.2, 'duel_amp0': 0.3, 'duel_freq0': 0.25, 'duel_ease': 1.6, 'duel_counter': 0.7, 'duel_min_blade': 2,
            'duel_clash_min': 2.0, 'duel_clash_max': 4.0, 'duel_probe_min': 5.0, 'duel_probe_max': 10.0,
            'duel_idle': 1.0, 'duel_pulse_hz': 3.0, 'duel_pulse_depth': 0.15, 'duel_flicker': 0.3, 'duel_peak_only': 1,
            'hyper_cycle': 10, 'hyper_jump': 7.4, 'hyper_speed': 24, 'hyper_tail': 6, 'hyper_density': 1.0,
            'march_bpm': 104, 'march_block': 3, 'march_flow': 4.0, 'march_gap': 0.25, 'march_floor': 0.06, 'march_gable': 0.6,
            'hg_base': 0.15, 'hg_wave': 18.0, 'hg_speed': 1.5, 'hg_white': 0.12, 'hg_white_wave': 0.0, 'hg_dir': 0,
            'hg_green_rgb': 0x00ff00, 'hg_purple_rgb': 0x8b00ff, 'hg_white_rgb': 0xffffff}   # *_rgb = colour knobs, 0xRRGGBB
PARAMS = dict(DEFAULTS)
try: PARAMS.update({k: float(v) for k, v in json.load(open(PARAMS_PATH)).items() if k in DEFAULTS})
except Exception: pass

# ---------------------------------------------------------------- zones (config.json)
def span(a, b): return list(range(a, b + 1)) if b >= a else list(range(a, b - 1, -1))
Z = CFG.get('zones', {})
OFF_SET = set(Z.get('off', []))
ALL = [i for i in range(N) if i not in OFF_SET]
def lit(leds): return [i for i in leds if i not in OFF_SET]
ARCHES = [tuple(sorted(r)) for r in Z.get('arches', [])]        # gables / peaks: a blade grows from each foot
DUEL_ARCHES = ARCHES or [(ALL[0], ALL[-1])]                     # no arches defined: one duel over the whole strip
GABLE = lit(i for lo, hi in ARCHES for i in range(lo, hi + 1))
GABLE_SET = set(GABLE)
PEAK_AT = [(lo, hi, (lo + hi) / 2) for lo, hi in ARCHES]        # each arch and where its peak sits (between two LEDs for an even count)
PEAKS = [i for lo, hi in ARCHES for i in (lo + (hi - lo + 1) // 2 - 1, lo + (hi - lo + 1) // 2)]   # the LEDs straddling each peak
DOOR = lit(span(*Z['door'])) if Z.get('door') else []
RUNWAYS = [span(a, b) for a, b in (Z.get('runways') or [[ALL[0], ALL[-1]]])]
RUN = {}                                                        # runway LED -> distance from its target end (1 = at the target)
for leds in RUNWAYS:
    for k, i in enumerate(leds):
        if i not in OFF_SET: RUN[i] = len(leds) - k
ENTRY = set(RUN) | set(DOOR)                                    # the runways plus the door they lead to
SIDE_RUNS = [r for r in (lit(span(a, b)) for a, b in Z.get('sparkle', [])) if r]   # extra star field for Hyperspace (side walls)
SIDES = [i for r in SIDE_RUNS for i in r]
def downhill(i):
    """Distance along the strip from the nearest peak: down both sides of every arch and on round the corners along the
    walls and runways - the whole-roof 'Downhill' direction. Without arches it is just the LED index."""
    return min(abs(i - pk) for _, _, pk in PEAK_AT) - 0.5 if PEAK_AT else float(i)
# Named zones for the theme builder (the page lists them from the status attribute `zones`)
ZONES = {'All': ALL}
for k, (lo, hi) in enumerate(ARCHES): ZONES['Arch %d (%d-%d)' % (k + 1, lo, hi)] = lit(range(lo, hi + 1))
if len(ARCHES) > 1: ZONES['All arches'] = GABLE
if Z.get('runways'):
    for k, leds in enumerate(RUNWAYS): ZONES['Runway %d (%d-%d)' % (k + 1, leds[0], leds[-1])] = lit(leds)
    if len(RUNWAYS) > 1: ZONES['All runways'] = sorted(RUN)
if DOOR: ZONES['Door (%d-%d)' % (DOOR[0], DOOR[-1])] = DOOR
for k, r in enumerate(SIDE_RUNS): ZONES['Side %d (%d-%d)' % (k + 1, r[0], r[-1])] = r
if len(SIDE_RUNS) > 1: ZONES['All sides'] = SIDES

# Reset / Save settings on the page touch only the shown effect's knobs (fps + gain are shared, left alone)
PARAM_GROUPS = {'Hyperspace': ('hyper_',), 'Imperial March': ('march_',), 'Saber Duel': ('duel_',), 'Halloween Glow': ('hg_',),
                'Duel + March': ('duel_', 'march_')}
def group_keys(effect): return [k for k in DEFAULTS if k.startswith(PARAM_GROUPS.get(effect, ('-',)))]
OFF, WHITE, RED, BLUE = (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 40, 255)

_tls = threading.local()                                        # the preview thread renders with its own trial parameters
def P(k): return getattr(_tls, 'prm', PARAMS).get(k, DEFAULTS[k])
def scale(c, k): k = max(0.0, min(1.0, k)); return (int(c[0] * k), int(c[1] * k), int(c[2] * k))
def rgbp(k): v = int(P(k)); return (v >> 16 & 255, v >> 8 & 255, v & 255)     # colour knob -> (r, g, b)
def mix(a, b, t): t = max(0.0, min(1.0, t)); return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
def log(*a): print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)
def blank_off(cols):
    if len(cols) == N:
        for i in OFF_SET: cols[i] = OFF
    return cols

# ---------------------------------------------------------------- LAN transport
_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
def send(obj): _sock.sendto(json.dumps(obj).encode(), (IP, 4003))
def xor(b):
    x = 0
    for v in b: x ^= v
    return x
def _pt(b): body = bytes(b); send({"msg": {"cmd": "razer", "data": {"pt": base64.b64encode(body + bytes([xor(body)])).decode()}}})
def razer(colors, stretch=0): _pt(bytes([0xBB, 0x00, 2 + 3 * len(colors), 0xB0, stretch, len(colors)]) + b''.join(bytes(c) for c in colors))
def razer_on(): _pt([0xBB, 0x00, 0x01, 0xB1, 0x01])     # 'uwABsQEK'
def razer_off(): _pt([0xBB, 0x00, 0x01, 0xB1, 0x00])    # 'uwABsQAL'
def lan_turn(on): send({"msg": {"cmd": "turn", "data": {"value": 1 if on else 0}}})
def lan_brightness(pct): send({"msg": {"cmd": "brightness", "data": {"value": max(1, min(100, int(pct)))}}})

# ---------------------------------------------------------------- Home Assistant
def _tok(): return SUP or open(CFG['ha_token_file']).read().strip()
def ha(method, path, body=None):
    req = urllib.request.Request(HA + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={'Authorization': 'Bearer ' + _tok(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=4) as r: return json.loads(r.read() or b'null')
_ha_down = False
def ha_state(entity):
    global _ha_down
    try: r = ha('GET', '/api/states/' + entity)
    except Exception as e:
        if not _ha_down: log('HA unreachable, lights keep running:', entity, e)
        _ha_down = True; return None
    if _ha_down: log('HA reachable again')
    _ha_down = False; return r
class Latest(threading.Thread):
    """Runs fn on its own thread with only the newest put() arguments, so a slow or dead HA / network share can never stall the frame loop."""
    def __init__(s, fn): super().__init__(daemon=True); s.fn = fn; s.v = None; s.ev = threading.Event(); s.start()
    def put(s, *a, **k): s.v = (a, k); s.ev.set()
    def run(s):
        while True:
            s.ev.wait(); s.ev.clear(); v, s.v = s.v, None
            if v:
                try: s.fn(*v[0], **v[1])
                except Exception as e: log('background write failed:', e)
def _ha_status_now(mode, **attrs):
    ha('POST', '/api/states/' + STATUS, {'state': mode, 'attributes': {'friendly_name': 'Govee realtime engine', 'icon': 'mdi:star-shooting', 'host': socket.gethostname(), **attrs}})
_status_bg = Latest(_ha_status_now)
def ha_status(mode, **attrs): _status_bg.put(mode, **attrs)
def _write_frame(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f: json.dump(obj, f)
    os.replace(tmp, path)
_frame_bg, _preview_bg = Latest(_write_frame), Latest(_write_frame)
HIST_S = 1.2      # each frame file also carries the last HIST_S s of frames ('hist': [[t, px], ...]) so the page can replay them on an
_hist = {}        # even clock: writes to a network share stall unpredictably (up to ~350 ms measured), which read as stutter.
def publish_frame(mode, cols, path=None, write=True):
    path = path or FRAME_PATH; t = round(time.time(), 3); px = ''.join('%02x%02x%02x' % c for c in cols)
    h = _hist.setdefault(path, [])
    if not cols or (h and h[-1][2] != mode): h.clear()
    if cols: h.append((t, px, mode))
    while h and h[0][0] < t - HIST_S: h.pop(0)
    if write: (_preview_bg if path == PREVIEW_PATH else _frame_bg).put(path, {'t': t, 'mode': mode, 'px': px, 'hist': [[a, b] for a, b, _ in h]})

class Preview(threading.Thread):
    """Renders an effect with trial parameters into govee_preview.json. Never touches the strip."""
    def __init__(s): super().__init__(daemon=True); s.mode = None; s.prm = dict(PARAMS); s.eff = None; s.t0 = 0.0; s.lock = threading.Lock()
    def set_mode(s, mode):
        with s.lock:
            s.mode = mode if mode in MODES else None; s.eff = MODES[mode]() if s.mode else None; s.t0 = time.time()
        if not s.mode: publish_frame('Off', [], PREVIEW_PATH)
    def set_design(s, spec, fx=None):   # builder edits keep the running clock, so tweaking a slider doesn't restart the motion
        with s.lock:
            if fx:                                                   # controller-theme preview: the strip's own effect, imitated
                if not isinstance(s.eff, DiyFx): s.t0 = time.time()
                s.eff = DiyFx(spec, fx)
            elif isinstance(s.eff, Custom): s.eff.set(spec)
            else: s.eff = Custom(spec); s.t0 = time.time()
            s.mode = 'Custom'
    def run(s):
        _tls.prm = s.prm; n = 0; nxt = time.time()
        while True:
            with s.lock: eff, mode = s.eff, s.mode
            if eff:
                try: publish_frame(mode, roof_view(blank_off(eff.frame(time.time() - s.t0, 0.05))), PREVIEW_PATH, write=n % 2 == 0)   # 20 fps, file at 10 Hz
                except Exception as e: log('preview error:', e)
                n += 1; nxt += 0.05
                if nxt < time.time() - 0.2: nxt = time.time()          # fell behind (stall): resync instead of racing to catch up
                time.sleep(max(0.0, nxt - time.time()))
            else: time.sleep(0.3); nxt = time.time()

# ---------------------------------------------------------------- effects: frame(t, dt) -> list of N colours
class Hyperspace:
    """Blue-white stars stream down the runways toward their target end, speed up and stretch into streaks, then the jump:
    a white flash and a blue hyperspace glow that fades. Arches and sparkle zones twinkle as the star field."""
    FLASH = 0.18
    def __init__(s):
        s.stars = []; s.next = 0.0; s.cycle = -1; s.far = max(RUN.values()) + 1
        s.ph = {i: random.uniform(0, 6.28) for i in GABLE + SIDES}
    def frame(s, t, dt):
        T, JUMP = P('hyper_cycle'), min(P('hyper_jump'), P('hyper_cycle') - 1)
        c = int(t // T); t %= T
        if c != s.cycle: s.cycle = c; s.stars = []; s.next = t
        prog = min(1.0, t / JUMP)
        cols = [OFF] * N
        if t < JUMP:
            v, tail = 2.5 + P('hyper_speed') * prog ** 2.2, 0.5 + P('hyper_tail') * prog ** 2
            if t >= s.next: s.stars.append(s.far + random.random() * 2); s.next = t + max(0.07, 0.5 - 0.42 * prog) / max(0.2, P('hyper_density'))
            s.stars = [p - v * dt for p in s.stars if p - v * dt > -tail]
            gain = 0.45 + 0.55 * prog
            for led, d in RUN.items():
                b = 0.0
                for p in s.stars:
                    if p <= d <= p + tail: b = max(b, (1 - (d - p) / tail) ** 1.5)
                cols[led] = scale(mix((120, 160, 255), WHITE, b * 0.6), b * gain)
            for i in DOOR: cols[i] = scale((60, 90, 200), 0.15 + 0.6 * prog)
            for i in GABLE + SIDES:
                tw = (0.5 * (1 + math.sin(6.28 * (0.25 + 1.5 * prog) * t + s.ph[i]))) ** 3
                cols[i] = scale((150, 170, 255), 0.06 + 0.3 * tw)
        elif t < JUMP + s.FLASH:
            for i in list(RUN) + DOOR + GABLE + SIDES: cols[i] = WHITE
        else:
            k = 1 - (t - JUMP - s.FLASH) / max(0.1, T - JUMP - s.FLASH)
            for i in RUN: cols[i] = scale((30, 60, 180), 0.5 * k)
            for i in DOOR: cols[i] = scale((80, 120, 255), k)
            for i in GABLE + SIDES: cols[i] = scale((60, 80, 200), 0.3 * k)
        return cols

class March:
    """The Imperial March, visually: every note is a red pulse, marching blocks flow along the runways toward their target,
    the door zone flashes white on the three opening notes of each phrase, the arches breathe deep red. Two phrases."""
    RH = [(1, 1.0), (1, 1.0), (1, 1.0), (0.75, 0.8), (0.25, 0.5), (1, 1.0), (0.75, 0.8), (0.25, 0.5), (2, 1.0)]   # (beats, accent)
    def __init__(s, gables=True): s.step = 0; s.last = -1; s.gables = gables
    def frame(s, t, dt):
        beat = 60 / max(30, P('march_bpm')); notes, b = [], 0.0
        for _ in range(2):
            for beats, acc in s.RH: notes.append((b * beat, beats * beat, acc)); b += beats
        T = b * beat; tt = t % T
        idx = max(i for i, (on, dur, acc) in enumerate(notes) if on <= tt)
        if idx != s.last: s.last = idx; s.step += 1
        on, dur, acc = notes[idx]; age = tt - on
        gap, floor = P('march_gap'), P('march_floor')
        if age < dur * (1 - gap): lvl = acc * (1.0 if dur < 1.5 else max(0.35, 1 - age / dur))   # note ON: clean flash, long notes fade
        else: lvl = floor                                                                            # short dark gap before the next note
        blk = int(P('march_block')); off = t * P('march_flow')                     # runway: blocks flow toward the target end
        cols = [OFF] * N
        for led, d in RUN.items(): cols[led] = scale(RED, lvl if blk < 2 or int(d + off) % blk == 0 else lvl * 0.3)
        door = WHITE if idx % 9 < 3 and age < dur * (1 - gap) else scale(RED, lvl)
        for i in DOOR: cols[i] = door
        if s.gables:
            for i in GABLE: cols[i] = scale(RED, 0.12 + P('march_gable') * lvl)
            if idx % 9 == 0 and age < 0.15:
                for i in PEAKS: cols[i] = WHITE
        return cols

class Duel:
    """Lightsaber duel on each arch. Red blade from one foot, light-blue blade from the other, uniform intensity.
    Cycle: clash (duel_clash_min..max s, on one of the two LEDs straddling the peak: white centre, light red / light blue
    either side, hard bright flicker, blades pulse gently) -> retract (both blades snap back to duel_min_blade) ->
    build (duel_probe_min..max s: the blades trade swings that grow bigger and faster while the gap closes) -> clash."""
    SABER_RED, SABER_BLUE = (255, 0, 0), (70, 150, 255)
    HIT_RED, HIT_BLUE = (255, 110, 110), (160, 210, 255)
    def __init__(s):
        s.f = []
        for lo, hi in DUEL_ARCHES:
            L = hi - lo + 1; s.f.append({'lo': lo, 'hi': hi, 'L': L, 'r': 2.0, 'b': 2.0, 'c': L // 2, 'state': 'retract', 'timer': 0.0,
                                         'phase': random.uniform(0, 6.28), 'ph': 0.0, 'D': 1.0})
    def _start_build(s, f, t):
        L = f['L']; minb = max(1, P('duel_min_blade')); peak_only = P('duel_peak_only') >= 0.5
        f['c'] = random.choice((L // 2 - 1, L // 2)) if peak_only else random.randint(int(minb), int(L - minb - 1))
        f['state'] = 'build'; f['t0'] = t; f['D'] = random.uniform(P('duel_probe_min'), max(P('duel_probe_min'), P('duel_probe_max')))
    def frame(s, t, dt):
        cols = [OFF] * N
        minb, idle, hz, depth, flick = max(1, P('duel_min_blade')), P('duel_idle'), P('duel_pulse_hz'), P('duel_pulse_depth'), P('duel_flicker')
        amp_max, freq_max, retract_v = P('duel_amp'), P('duel_freq'), P('duel_speed')
        for f in s.f:
            L = f['L']; f['timer'] -= dt
            if f['state'] == 'retract':
                f['r'] = max(minb, f['r'] - retract_v * dt); f['b'] = max(minb, f['b'] - retract_v * dt)
                if f['r'] <= minb and f['b'] <= minb: s._start_build(f, t)
            elif f['state'] == 'build':
                u = min(1.0, (t - f['t0']) / max(0.5, f['D'])); e = u ** max(0.3, P('duel_ease'))   # slow start, accelerating
                freq = P('duel_freq0') + (freq_max - P('duel_freq0')) * e; amp = P('duel_amp0') + (amp_max - P('duel_amp0')) * e
                f['phase'] += 6.283 * freq * dt; sw = math.sin(f['phase'])
                rmax, bmax = f['c'], L - f['c'] - 1                                            # blade lengths at the clash
                f['r'] = max(minb, min(rmax, minb + (rmax - minb) * e + amp * sw))             # red lunges while blue gives way
                f['b'] = max(minb, min(bmax, minb + (bmax - minb) * e - amp * P('duel_counter') * sw))
                if u >= 1.0:
                    f['r'], f['b'] = rmax, bmax; f['state'] = 'clash'; f['ph'] = t
                    f['timer'] = random.uniform(P('duel_clash_min'), max(P('duel_clash_min'), P('duel_clash_max')))
            elif f['state'] == 'clash' and f['timer'] <= 0: f['state'] = 'retract'
            clash = f['state'] == 'clash'
            lvl = idle if not clash else (1 - depth) + depth * 0.5 * (1 + math.sin(6.28 * hz * (t - f['ph'])))   # whole blades breathe gently
            for i in range(f['lo'], f['hi'] + 1):
                x = i - f['lo'] + 0.5
                cols[i] = scale(s.SABER_RED, lvl) if x < f['r'] else scale(s.SABER_BLUE, lvl) if x > L - f['b'] else OFF
            if clash:                                                              # impact: white centre, light red / light blue either side
                c = f['lo'] + f['c']
                for i, col in ((c - 1, s.HIT_RED), (c, WHITE), (c + 1, s.HIT_BLUE)):
                    if f['lo'] <= i <= f['hi']: cols[i] = scale(col, random.uniform(1 - flick, 1.0))   # bright <-> super bright, never dark
        return cols

class Sweep:
    """One white LED at a time, half a second each (status shows the LED number) - for mapping your layout."""
    DWELL = 0.5
    def __init__(s): s.led = -1
    def frame(s, t, dt):
        s.led = int(t / s.DWELL) % N
        return [WHITE if i == s.led else OFF for i in range(N)]

class Manual:
    """Test-page patterns on an arbitrary pixel count: fill / alt / grad / chase / mask."""
    def __init__(s, mode, n, rgb, arg):
        s.mode, s.n, s.rgb, s.arg, s.led = mode, max(1, min(N_DEV, n)), rgb, arg, None
        s.mask = int(arg or '0', 16) if mode == 'mask' else 0
        s.period = max(0.1, float(arg or 1.0)) if mode == 'chase' else 1.0
    def frame(s, t, dt):
        n, c = s.n, s.rgb
        if s.mode == 'fill': return [c] * n
        if s.mode == 'alt': return [c if i % 2 == 0 else OFF for i in range(n)]
        if s.mode == 'grad': return [mix((255, 0, 0), (0, 0, 255), i / max(1, n - 1)) for i in range(n)]
        if s.mode == 'chase': s.led = int(t / s.period) % n; return [c if i == s.led else OFF for i in range(n)]
        if s.mode == 'mask': return [c if s.mask >> i & 1 else OFF for i in range(n)]
        return [OFF] * n

class DuelMarch:
    """Saber Duel on the arches + Imperial March on the runways and door zone."""
    def __init__(s): s.duel = Duel(); s.march = March(gables=False); s.duel_leds = {i for lo, hi in DUEL_ARCHES for i in range(lo, hi + 1)}
    def frame(s, t, dt):
        a = s.duel.frame(t, dt); b = s.march.frame(t, dt)
        return [a[i] if i in s.duel_leds else b[i] for i in range(N)]

class HalloweenGlow:
    """Colour 1, in-between, colour 2, in-between... (colours are knobs; defaults green / dim white / purple) under slow
    waves of brightness: three sine waves with unrelated lengths, speeds and directions add up to soft swells that drift
    along the roof and never quite repeat. Nothing jumps frame to frame and nothing drops below hg_base; the in-between
    LEDs sit at hg_white of that. (Pop-up glows and dying-bulb stutter read as flicker on a roofline - this is calmer.)"""
    def __init__(s):   # (length factor, speed factor incl. direction, phase) per wave
        s.waves = [(f, random.choice((-1, 1)) * random.uniform(0.6, 1.4), random.uniform(0, 6.283)) for f in (1.0, 0.63, 1.7)]
    @staticmethod
    def flow(mode, i):
        """hg_dir: 0 mixed | 1 along the strip | 2 reversed | 3 down the arches | 4 along the runways to the door | 5 arches +
        runways (downhill). Returns the distance the waves travel along for LED i (they flow toward larger values), or None = mixed."""
        if mode == 1: return i
        if mode == 2: return -i
        if mode == 3: return downhill(i) if i in GABLE_SET else None
        if mode == 4: return -RUN.get(i, 0) if i in ENTRY else None
        if mode == 5: return downhill(i)
        return None
    def frame(s, t, dt):
        L, v, base, mode = max(2.0, P('hg_wave')), P('hg_speed'), P('hg_base'), int(P('hg_dir'))
        pal = [(rgbp('hg_green_rgb'), 1.0), (rgbp('hg_white_rgb'), P('hg_white')), (rgbp('hg_purple_rgb'), 1.0), (rgbp('hg_white_rgb'), P('hg_white'))]
        cols = [OFF] * N
        for i in range(N):
            d = s.flow(mode, i)
            if d is None: w = sum(math.sin(6.283 / (L * f) * (i - sp * v * t) + ph) for f, sp, ph in s.waves) / 3   # -1..1, mostly +-0.6
            else: w = sum(math.sin(6.283 / (L * f) * (d - abs(sp) * v * t) + ph) for f, sp, ph in s.waves) / 3
            w = max(0.0, min(1.0, 0.5 + 0.8 * w)) ** 1.5                                               # 0..1, darker troughs
            lvl = base + (1 - base) * w
            if i % 2: lvl = 1 - P('hg_white_wave') * (1 - lvl)          # in-betweens: 0 = steady at hg_white, 1 = ride the waves
            c, k = pal[i % 4]; cols[i] = scale(c, k * lvl)
        return cols

# ---------------------------------------------------------------- theme builder (effect 'Custom')
def hexrgb(h): h = str(h).lstrip('#'); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
def zone_leds(sp):
    """A layer's LEDs: a named zone (see ZONES), or zone 'Custom' with leds like '0-12, 40-44' (0-based, power end first)."""
    if sp['zone'] != 'Custom': return ZONES.get(sp['zone'], ALL)
    out = set()
    for part in sp['leds'].replace(' ', '').split(','):
        a, _, b = part.partition('-')
        try: lo = int(a); hi = int(b) if b else lo
        except ValueError: continue
        out.update(range(max(0, min(lo, hi)), min(N - 1, max(lo, hi)) + 1))
    return [i for i in sorted(out) if i not in OFF_SET]
def clean_layer(sp):
    """One builder layer from the page -> safe types (a bad value falls back to the default instead of killing frames)."""
    sp = sp if isinstance(sp, dict) else {}; out = dict(DEFAULT_LAYER)
    for k, v in DEFAULT_LAYER.items():
        try:
            if k == 'colors': out[k] = ['%02x%02x%02x' % hexrgb(c) for c in sp[k]][:8] or v
            elif isinstance(v, float): out[k] = float(sp[k])
            elif k in sp: out[k] = str(sp[k])[:200]
        except Exception: pass
    return out
def clean_spec(sp):
    sp = sp if isinstance(sp, dict) else {}
    layers = sp['layers'] if isinstance(sp.get('layers'), list) else [sp]   # early designs were one flat layer
    return {'layers': [clean_layer(l) for l in layers[:8]] or [clean_layer({})]}
DEFAULT_LAYER = {'zone': 'All', 'leds': '', 'motion': 'Random waves', 'colors': ['00ff00', '1f1f1f', '8b00ff', '1f1f1f'], 'pattern': 'Repeat', 'group': 1.0,
                 'speed': 1.5, 'size': 18.0, 'floor': 0.15, 'bri': 1.0, 'density': 0.3, 'dir': 'Forward', 'peaks': 'Follow', 'entry': 'Follow'}
MOTIONS = ('Static', 'Breathe', 'Wave', 'Random waves', 'Scroll colours', 'Chase', 'Comet', 'Twinkle', 'Sparkle', 'Colour fade')

class Custom:
    """Theme builder design: layers painted in order, each on its own zone (later layers win where zones overlap)."""
    def __init__(s, spec=None): s.layers = []; s.set(spec or DESIGNS.get('_last'))
    def set(s, spec):   # keeps each layer's random state, so editing a design doesn't reshuffle twinkles
        s.spec = clean_spec(spec); old = s.layers
        s.layers = [old[k] if k < len(old) else Layer() for k in range(len(s.spec['layers']))]
        for L, sp in zip(s.layers, s.spec['layers']): L.spec = sp; L.leds = zone_leds(sp)
    def frame(s, t, dt):
        cols = [OFF] * N
        for L in s.layers: L.frame(t, dt, cols)
        return cols

class Layer:
    """A palette laid along the roof (Repeat / Blend / Random, `group` LEDs per colour) under one motion, on the layer's
    LEDs. Motions travel toward larger coord(): along the strip (dir Forward / Reverse), or Downhill / Uphill = away from /
    toward the nearest peak everywhere, with per-zone overrides for the arches (peaks: Down = away from the peak) and the
    runways + door (entry: To door / From door). speed ~ LEDs/s, size = wave / block / comet length in LEDs, floor = dimmest
    level, density = share of LEDs that twinkle / sparkle."""
    def __init__(s):
        s.spec, s.leds = clean_layer({}), ALL
        s.rnd = [random.random() for _ in range(N)]; s.pick = [random.random() for _ in range(N)]; s.ph = [random.uniform(0, 6.283) for _ in range(N)]
        s.spark = [0.0] * N; s.waves = HalloweenGlow().waves
    @staticmethod
    def coord(sp, i):
        if sp['dir'] in ('Downhill', 'Uphill'): d = downhill(i); return d if sp['dir'] == 'Downhill' else -d
        for lo, hi, pk in PEAK_AT:
            if lo <= i <= hi and sp['peaks'] != 'Follow': d = abs(i - pk) - 0.5; return d if sp['peaks'] == 'Down' else -d
        if i in ENTRY and sp['entry'] != 'Follow': d = RUN.get(i, 0); return -d if sp['entry'] == 'To door' else d   # door LEDs = the target
        return -i if sp['dir'] == 'Reverse' else i
    def colour(s, pal, x, i):
        sp, k = s.spec, len(pal)
        if sp['pattern'] == 'Random': return pal[int(s.rnd[i] * k) % k]
        p = x / max(1.0, sp['group']); a = math.floor(p)
        return mix(pal[int(a) % k], pal[(int(a) + 1) % k], p - a) if sp['pattern'] == 'Blend' else pal[int(a) % k]
    def frame(s, t, dt, cols):
        sp = s.spec; pal = [hexrgb(c) for c in sp['colors']]
        m, v, L, floor, dens = sp['motion'], sp['speed'], max(1.0, sp['size']), sp['floor'], sp['density']
        fp = t * v / 10; fade = mix(pal[int(fp) % len(pal)], pal[(int(fp) + 1) % len(pal)], min(1.0, 2 * (fp % 1)))   # Colour fade: blend, then hold
        for i in s.leds:
            d = s.coord(sp, i)
            c = fade if m == 'Colour fade' else s.colour(pal, d - v * t if m == 'Scroll colours' else d, i)
            if m == 'Breathe': w = 0.5 - 0.5 * math.cos(6.283 * fp)
            elif m == 'Wave': w = (0.5 + 0.5 * math.sin(6.283 * (d - v * t) / L)) ** 1.5
            elif m == 'Random waves':
                w = sum(math.sin(6.283 / (L * f) * (d - abs(sp_) * v * t) + ph) for f, sp_, ph in s.waves) / 3
                w = max(0.0, min(1.0, 0.5 + 0.8 * w)) ** 1.5
            elif m == 'Chase': w = 1.0 if (d - v * t) % L < L / 2 else 0.0
            elif m == 'Comet': w = max(0.0, 1 - ((v * t - d) % (2 * L)) / L) ** 2
            elif m == 'Twinkle': w = (0.5 + 0.5 * math.sin(6.283 * (0.5 + s.rnd[i]) * v / 10 * t + s.ph[i])) ** 3 if s.pick[i] < dens else 0.0
            elif m == 'Sparkle':
                s.spark[i] = 1.0 if random.random() < dens * 0.5 * dt else max(0.0, s.spark[i] - dt * max(0.2, v))
                w = s.spark[i]
            else: w = 1.0
            cols[i] = scale(c, sp['bri'] * (floor + (1 - floor) * w))

MODES = {'Hyperspace': Hyperspace, 'Imperial March': March, 'Saber Duel': Duel, 'Duel + March': DuelMarch, 'LED Sweep': Sweep,
         'Halloween Glow': HalloweenGlow, 'Custom': Custom}   # Custom = the theme builder's last design sent to the roof
DESIGNS = {}                                                   # saved builder designs by name; '_last' = last sent to the roof
try: DESIGNS = json.load(open(DESIGNS_PATH))
except Exception: pass
def save_designs(): json.dump(DESIGNS, open(DESIGNS_PATH, 'w'), indent=1)
def saved_names(): return sorted(k for k in DESIGNS if not k.startswith('_'))

# Controller (DIY) themes saved from the builder: the painted colours + one of the strip's own effects, uploaded by
# govee_diy.py on Home Assistant (which reads the same file through ha_www_dir) when 'DIY: <name>' is applied. Runs with the PC off.
DIY_EFFECTS = ('Cycle', 'Clockwise', 'Counterclockwise', 'Twinkle', 'Gradient', 'Breathe')   # codes live in govee_diy.py
def diy_load():
    try: return json.load(open(DIY_PATH))
    except FileNotFoundError: return {}
DIY = {}
try: DIY = diy_load()
except Exception as e: log('diy themes not loaded:', e)
def diy_update(fn):
    """Re-read the file, change it, write it back: never overwrite it with a stale in-memory copy (the share may have been down at boot)."""
    global DIY
    cur = diy_load(); fn(cur); _write_frame(DIY_PATH, cur); DIY = cur
def diy_groups(spec):
    """Painted colours of a design (motion ignored, brightness kept) -> [[rgb, [leds]], ...] for govee_diy.py; unlit LEDs show its background."""
    cols = Custom({'layers': [dict(l, motion='Static') for l in clean_spec(spec)['layers']]}).frame(0, 0.05)
    by = {}
    for i in ALL:
        if cols[i] != OFF: by.setdefault(cols[i], []).append(i)
    return [[list(c), leds] for c, leds in sorted(by.items(), key=lambda kv: kv[1][0])]   # ponytail: no cap on colours (Blend = one group per LED); the strip's packet limit is untested
def parse_fx(a):
    """The page's controller-theme settings {effect, speed, pct} -> (effect, speed 0-100, strip %) with safe fallbacks."""
    fx = a.get('fx') if isinstance(a.get('fx'), dict) else {}
    eff = fx.get('effect') if fx.get('effect') in DIY_EFFECTS else 'Cycle'
    try: return eff, max(0, min(100, int(float(fx.get('speed', 7))))), max(1, min(100, int(float(fx.get('pct', 40)))))
    except (TypeError, ValueError): return eff, 7, 40

class DiyFx:
    """Preview of a controller (DIY) theme: the painted colours animated the way the strip's own effects were seen to move
    on a camera. Cycle = every run of colour takes the previous run's colour (colours step toward higher LEDs) each hold;
    Clockwise / Counterclockwise = the pattern steps one LED forward / back; Breathe = fade to black and back; Twinkle =
    brief blackouts of most LEDs; Gradient = a centre-to-centre blended copy drifting forward (approximate). LEDs in
    zones.off stay dark. Brightness = strip % relative to the strip's usual roof_bri, so 40 % looks like the realtime preview."""
    FADE = 0.2                                                   # cross-fade seen between steps
    # (speed 0, speed 100) of each effect's clock, interpolated exponentially. The slow ends were measured on the strip; the
    # fast ends are a guess (the fastest app speed is a strobe). Not re-measured - treat as approximate.
    CYCLE, STEP, BREATH, TWINK, DRIFT = (4.0, 0.1), (1.8, 0.06), (12.0, 1.0), (3.0, 0.1), (0.05, 8.0)   # s hold, s/LED, s/breath, s/blackout, LEDs/s
    def per(s, lo_hi): return lo_hi[0] * (lo_hi[1] / lo_hi[0]) ** (s.speed / 100.0)
    def __init__(s, spec, fx):
        s.eff, s.speed, s.pct = fx; s.idx = ALL; s.n = len(ALL)
        full = Custom({'layers': [dict(l, motion='Static') for l in clean_spec(spec)['layers']]}).frame(0, 0.05)
        s.base = [full[i] for i in s.idx]
        s.runs = []                                              # [start, length, colour] of each same-colour run along the strip
        for i, c in enumerate(s.base):
            if s.runs and s.runs[-1][2] == c: s.runs[-1][1] += 1
            else: s.runs.append([i, 1, c])
        s.grad = s.blend(); s.rnd = random.Random(); s.blink = (-1, set())
        s.k = min(1.0, s.pct / max(1.0, P('roof_bri')))
    def blend(s):
        n, L = len(s.runs), s.n
        if n < 2: return list(s.base)
        ctr = [a + (ln - 1) / 2 for a, ln, _ in s.runs]; out = list(s.base)
        for r, (a, ln, c) in enumerate(s.runs):
            for i in range(a, a + ln):
                if i >= ctr[r]: q = (r + 1) % n; c2 = ctr[q] + (L if q < r else 0); out[i] = mix(c, s.runs[q][2], (i - ctr[r]) / max(0.5, c2 - ctr[r]))
                else: q = (r - 1) % n; c2 = ctr[q] - (L if q > r else 0); out[i] = mix(s.runs[q][2], c, (i - c2) / max(0.5, ctr[r] - c2))
        return out
    def cycle(s, k):
        n = len(s.runs); cols = list(s.base)
        for r, (a, ln, _) in enumerate(s.runs):
            c = s.runs[(r - k) % n][2]
            for i in range(a, a + ln): cols[i] = c
        return cols
    def roll(s, k): return [s.base[(i - k) % s.n] for i in range(s.n)]
    def snap(s, fn, hold, t):
        k, f = divmod(t, hold); k = int(k); fade = min(s.FADE, hold / 2)
        return fn(k) if f >= fade else [mix(a, b, f / fade) for a, b in zip(fn(k - 1), fn(k))]
    def frame(s, t, dt):
        L = s.n
        if s.eff == 'Cycle': cols = s.snap(s.cycle, s.per(s.CYCLE), t)
        elif s.eff in ('Clockwise', 'Counterclockwise'):
            d = 1 if s.eff == 'Clockwise' else -1; cols = s.snap(lambda k: s.roll(d * k), s.per(s.STEP), t)
        elif s.eff == 'Gradient':
            x = t * s.per(s.DRIFT); g = s.grad
            cols = [mix(g[int(math.floor(i - x)) % L], g[(int(math.floor(i - x)) + 1) % L], (i - x) % 1) for i in range(L)]
        elif s.eff == 'Breathe': w = 0.5 - 0.5 * math.cos(6.283 * t / s.per(s.BREATH)); cols = [scale(c, w) for c in s.base]
        elif s.eff == 'Twinkle':
            T = s.per(s.TWINK); k = int(t / T)                                                                  # a blackout of most LEDs every T
            if k != s.blink[0]: s.blink = (k, {i for i in range(L) if s.rnd.random() < 0.7})
            cols = [OFF if t % T < min(0.15, T / 2) and i in s.blink[1] else c for i, c in enumerate(s.base)]
        else: cols = list(s.base)
        out = [OFF] * N
        for k, i in enumerate(s.idx): out[i] = scale(cols[k], s.k)
        return out

def ha_ws(msg):
    """One Home Assistant websocket call (input_select/update has no REST equivalent). Needs `pip install websocket-client`."""
    import websocket
    ws = websocket.create_connection(WS_URL, timeout=5)
    try:
        ws.recv(); ws.send(json.dumps({'type': 'auth', 'access_token': _tok()})); ws.recv()
        ws.send(json.dumps({'id': 1, **msg})); r = json.loads(ws.recv())
        if not r.get('success'): raise RuntimeError(r.get('error'))
        return r.get('result')
    finally: ws.close()
def sync_options():
    """Selector options = Off + effects + saved designs, so saved designs show up in the schedule ('RT: <name>').
    Updates the stored helper (survives HA restarts) and keeps the current selection."""
    want = ['Off'] + list(MODES) + saved_names()
    st = ha_state(SEL)
    if st and st['attributes'].get('options') != want:
        at = st['attributes']; keep = {'name': at.get('friendly_name', 'Govee realtime effect'), **({'icon': at['icon']} if at.get('icon') else {})}   # update wants the whole config
        try: ha_ws({'type': 'input_select/update', 'input_select_id': SEL.split('.')[1], 'options': want, **keep}); log('selector options ->', want)
        except Exception as e: log('selector options update failed (pip install websocket-client?):', e)

def parse_cmd(text):
    parts = [p for p in (text or '').split('|') if not p.startswith('#')]
    if len(parts) < 2 or parts[0] != 'v1': return None
    mode = parts[1].strip().lower(); n = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 24
    h = parts[3].strip() if len(parts) > 3 and parts[3].strip() else 'ffffff'
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else WHITE
    arg = parts[4].strip() if len(parts) > 4 else ''
    return mode, n, rgb, arg

def to_device(cols):
    """N design LEDs -> at most N_DEV pixels. With max_pixels >= leds that is 1:1. With fewer pixels than LEDs the strip spreads
    them evenly over the LEDs (neighbours light in pairs), so each pixel gets the average of the LEDs it covers."""
    if len(cols) <= N_DEV: return cols
    out = []
    for p in range(N_DEV):
        a = int(p * len(cols) / N_DEV); b = max(a + 1, int((p + 1) * len(cols) / N_DEV))
        out.append(tuple(sum(c[k] for c in cols[a:b]) // (b - a) for k in range(3)))
    return out

def roof_view(cols):
    """What the roof actually shows for a frame, as N LEDs (for the page / house map)."""
    if len(cols) == N and N <= N_DEV: return cols
    dev = to_device(cols); return [dev[int(i * len(dev) / N)] for i in range(N)]

# ---------------------------------------------------------------- runner
class Engine:
    def __init__(s):
        s.mode = 'Off'; s.eff = None; s.t0 = 0.0; s.frames = 0; s.since = None; s.cmd = None; s.last_enable = 0.0
        s.hold = 0.0; s.sel_seen = s.sel_live = 'Off'; s.cmd_live = None; s.des_live = {}; s.last_send = 0.0; s.gap_max = 0.0; s.nxt = 0.0
        s.preview = Preview(); s.preview.start()
    def start(s, mode, eff=None):
        if s.mode == 'Off':
            lan_turn(True); time.sleep(0.2)
            if RAZER_CTRL: razer_on(); time.sleep(0.2)
            s.hw_bri()
        s.last_enable = time.time()
        s.last_send = 0.0; s.gap_max = 0.0
        s.eff = eff or MODES[mode](); s.mode = mode; s.t0 = time.time(); s.frames = 0; s.since = datetime.datetime.now().isoformat(timespec='seconds')
        log('start', mode, getattr(s.eff, 'n', N_DEV), 'px'); s.status()
    def stop(s, restore=True):
        # never switches the strip off: Home Assistant's own schedule owns on/off
        if s.mode == 'Off': return
        log('stop', s.mode); s.eff = None; s.mode = 'Off'
        try:
            if RAZER_CTRL: razer_off(); time.sleep(0.3)
            lan_brightness(P('roof_bri')); time.sleep(0.2)
            applied = ha_state(APPLIED) if restore else None; name = applied and applied.get('state')
            if name and name.startswith('RT: '): name = DEFAULT_THEME   # a scheduled realtime theme: restoring it would restart us
            if name and name not in ('unknown', 'unavailable'): ha('POST', '/api/services/script/turn_on', {'entity_id': APPLY_SCRIPT, 'variables': {'theme': name}}); log('restore theme', name)
        except Exception as e: log('restore failed:', e)
        publish_frame('Off', []); s.status()
    def status(s):
        ha_status(s.mode, since=s.since, frames=s.frames, fps=P('fps'), n=getattr(s.eff, 'n', N_DEV) if s.eff else None, led=getattr(s.eff, 'led', None), cmd=s.cmd,
                  params=PARAMS, preview_mode=s.preview.mode, preview_params=s.preview.prm, zones=list(ZONES),
                  design=DESIGNS.get('_last'), designs={k: DESIGNS[k] for k in saved_names()}, gap_max_ms=round(1000 * s.gap_max),
                  diy={k: {'spec': v.get('spec'), 'fx': v.get('fx'), 'pct': v.get('pct')} for k, v in DIY.items()})   # the page's Start-from list ('DIY: name')
        s.gap_max = 0.0                                                    # worst gap between roof frames since the last status
    def save(s): json.dump(PARAMS, open(PARAMS_PATH, 'w'))
    def select(s, option):
        """Point the selector at what we're running without the change bouncing back to us as a request."""
        s.hold = time.time(); s.sel_live = s.sel_seen = option
        try: ha('POST', '/api/services/input_select/select_option', {'entity_id': SEL, 'option': option})
        except Exception as e: log('selector update failed', e)
    def handle_design(s, a):
        to, name = a.get('to'), re.sub(r"[^\w \-'.&+]", '', str(a.get('name') or ''))[:40].strip(); spec = clean_spec(a.get('spec')); log('design', to, name)
        if to == 'preview': s.preview.set_design(spec, parse_fx(a) if 'fx' in a else None)
        elif to == 'diy' and name and not name.startswith('_'):          # save a controller theme (+ apply it through HA like the schedule does)
            eff, speed, pct = parse_fx(a); groups = diy_groups(spec); ok = True
            try: diy_update(lambda d: d.__setitem__(name, {'pct': pct, 'fx': [eff, speed], 'groups': groups, 'spec': spec})); log('diy saved', name, eff, speed, pct, len(groups), 'colours')
            except Exception as e: ok = False; log('diy save failed:', e)
            if ok and a.get('apply'):
                if s.mode != 'Off': s.stop(restore=False); s.select('Off')   # the script would otherwise select Off and restart the old applied theme
                try: ha('POST', '/api/services/script/turn_on', {'entity_id': APPLY_SCRIPT, 'variables': {'theme': 'DIY: ' + name}}); log('apply DIY', name)
                except Exception as e: log('apply failed:', e)
        elif to == 'diydel' and name in DIY:
            try: diy_update(lambda d: d.pop(name, None)); log('diy deleted', name)
            except Exception as e: log('diy delete failed:', e)
        elif to == 'roof':
            DESIGNS['_last'] = spec; save_designs()
            if isinstance(s.eff, Custom): s.eff.set(spec); s.mode = 'Custom'
            else: s.start('Custom', Custom(spec))
            s.select('Custom')
        elif to == 'save' and name and name not in MODES and name != 'Off' and not name.startswith('_'):
            DESIGNS[name] = spec; save_designs(); sync_options()
        elif to == 'delete' and name in saved_names(): del DESIGNS[name]; save_designs(); sync_options()
        s.status()
    def handle_cmd(s, text):
        p = parse_cmd(text)
        if not p: log('bad cmd', text); return
        mode, n, rgb, arg = p; log('cmd', mode, n, rgb, arg)
        if mode == 'off':
            s.stop()
            try: ha('POST', '/api/services/input_select/select_option', {'entity_id': SEL, 'option': 'Off'})
            except Exception as e: log('selector reset failed', e)
        elif mode == 'bri':
            PARAMS['roof_bri'] = max(1, min(100, int(float(arg or 100)))); s.save()
            s.hw_bri() if s.eff else lan_brightness(PARAMS['roof_bri']); s.status()
        elif mode in ('set', 'pset'):
            k, _, v = arg.partition('=')
            if k not in DEFAULTS: log('unknown param', k)
            else:
                try: (PARAMS if mode == 'set' else s.preview.prm)[k] = float(v); log(mode, k, '=', v)
                except ValueError: log('bad value', arg)
                if mode == 'set': s.save()
                if mode == 'set' and k in ('gain', 'roof_bri') and s.eff: s.hw_bri()
            s.status()
        elif mode == 'reset': PARAMS.update({k: DEFAULTS[k] for k in group_keys(arg)}); s.save(); log('params reset', arg); s.status()
        elif mode == 'preview': s.preview.set_mode(arg); log('preview ->', s.preview.mode); s.status()          # arg = effect name or Off
        elif mode == 'preset': s.preview.prm.update({k: DEFAULTS[k] for k in group_keys(arg)}); log('preview params reset', arg); s.status()
        elif mode == 'apply': PARAMS.update({k: s.preview.prm[k] for k in group_keys(arg) if k in s.preview.prm}); s.save(); log('preview params saved', arg); s.status()
        elif mode in ('fill', 'alt', 'grad', 'chase', 'mask'): s.start('Manual', Manual(mode, n, rgb, arg))
        else: log('unknown cmd mode', mode)
    def run_frame(s):
        now = time.time(); t = now - s.t0; fps = max(5, min(40, P('fps')))
        if RAZER_CTRL and P('enable_every') > 0 and now - s.last_enable >= P('enable_every'): razer_on(); s.last_enable = now
        cols = blank_off(s.eff.frame(t, 1 / fps))
        razer(to_device(cols)); s.frames += 1                            # full range: gain is applied by the strip (hw_bri)
        if s.last_send: s.gap_max = max(s.gap_max, now - s.last_send)   # steadiness check, reported as gap_max_ms in the status
        s.last_send = now
        if P('gain') < 1: cols = [scale(c, P('gain')) for c in cols]     # the page still shows it dimmed
        publish_frame(s.mode, roof_view(cols), write=s.frames % 2 == 0)
        return fps
    def hw_bri(s): lan_brightness(max(1, round(P('roof_bri') * min(1.0, P('gain')))))
    def design_attrs(s):
        try: return ha('GET', '/api/states/' + DESIGN).get('attributes') or {}
        except Exception: return {}                                   # 404 until the page first writes it (and after an HA restart)
    def poll_ha(s):
        while True:
            t = time.time(); a, b, d = ha_state(SEL), ha_state(CMD), s.design_attrs()
            if a and t > s.hold: s.sel_live = a.get('state', s.sel_live)   # skip a read that started before our own select()
            if b: s.cmd_live = b.get('state', s.cmd_live)
            if d: s.des_live = d
            time.sleep(1.0)
    def start_named(s, name):
        if name in MODES: s.start(name)
        elif name in DESIGNS: s.start(name, Custom(DESIGNS[name]))   # a saved builder design
        else: s.stop()
    def serve(s):
        log('engine up, config', _cfg_path, 'N =', N, 'N_DEV =', N_DEV, 'following', SEL, 'and', CMD)
        st = ha_state(CMD); s.cmd = (st or {}).get('state')          # ignore whatever command is already there at boot
        publish_frame('Off', []); ha_status('Off', frames=0, cmd=s.cmd, params=PARAMS)
        sync_options()
        last_status = 0.0; s.sel_seen = (ha_state(SEL) or {}).get('state', 'Off'); s.sel_live, s.cmd_live = s.sel_seen, s.cmd
        s.des_live = s.design_attrs(); des_nonce = s.des_live.get('nonce')   # likewise the last design message
        threading.Thread(target=s.poll_ha, daemon=True).start()
        if s.sel_seen != 'Off': log('resuming selected effect after boot:', s.sel_seen); s.start_named(s.sel_seen)   # watchdog restarts continue a show
        while True:
            now = time.time()
            sel, cmd, des = s.sel_live, s.cmd_live, s.des_live
            if cmd is not None and s.cmd is None: s.cmd = cmd          # first successful read after a boot-time HA outage is the baseline, not a new command
            elif cmd != s.cmd: s.cmd = cmd; s.handle_cmd(cmd)
            elif des.get('nonce') != des_nonce: des_nonce = des.get('nonce'); s.handle_design(des)
            elif sel != s.sel_seen: s.start_named(sel)
            if s.hold < now: s.sel_seen = sel                         # unless select() just moved it this pass
            if s.eff:
                fps = 20
                try: fps = s.run_frame()
                except Exception as e: log('frame error:', e); time.sleep(1)
                if now - last_status >= (1.0 if getattr(s.eff, 'led', None) is not None else 5.0): last_status = now; s.status()
                s.nxt += 1 / fps                                            # fixed frame clock (relative sleeps drift and bunch up)
                if s.nxt < time.time() - 0.2: s.nxt = time.time()           # after a stall or a fresh start: resync, don't race
                time.sleep(max(0.0, s.nxt - time.time()))
            else:
                if now - last_status >= 60: last_status = now; s.status()   # idle too: the sensor (designs, diy lists) vanishes on an HA restart
                time.sleep(0.25)

def simulate(secs=30.0):
    """Offline check: renders every effect, test pattern, builder motion and controller-theme preview for secs of simulated
    time. Sends nothing, needs no Home Assistant, writes no files."""
    fps = 20; zone = [z for z in ZONES if z != 'All'][:1] or ['All']
    builders = [('Builder ' + m, lambda m=m, p=p: Custom({'layers': [dict(DEFAULT_LAYER, motion=m, pattern=p, dir='Downhill'),
                                                                     dict(DEFAULT_LAYER, zone=zone[0], motion=m, pattern=p, peaks='Down', entry='To door')]}))
                for m, p in zip(MOTIONS, ('Repeat', 'Blend', 'Random') * 4)]
    stripes = {'layers': [dict(DEFAULT_LAYER, motion='Static', colors=['ff0000', '00ff00', '0000ff'], group=3.0)]}
    controller = [('Controller ' + f, lambda f=f: DiyFx(stripes, (f, 100, 40))) for f in DIY_EFFECTS]
    manual = [('Manual ' + m, lambda m=m: Manual(m, 24, (255, 120, 0), '3' if m == 'mask' else '')) for m in ('fill', 'alt', 'grad', 'chase', 'mask')]
    for name, cls in list(MODES.items()) + builders + controller + manual:
        eff, seen = cls(), set()
        for f in range(int(secs * fps)):
            cols = blank_off(eff.frame(f / fps, 1 / fps))
            assert all(len(c) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in c) for c in cols), name + ': bad colour'
            assert len(to_device(cols)) <= N_DEV, name + ': too many pixels for the strip'
            seen.update(i for i, c in enumerate(cols) if max(c))
        assert seen, name + ': never lit anything'
        assert not (seen & OFF_SET) or name.startswith('Manual'), name + ': lit an LED from zones.off'
        print('%-26s ok  %3d LEDs lit at some point' % (name, len(seen)))
    assert group_keys('Duel + March') and not group_keys('LED Sweep')
    assert zone_leds(dict(DEFAULT_LAYER, zone='Custom', leds='0-2, 5, x, 9-7')) == [i for i in (0, 1, 2, 5, 7, 8, 9) if i in ALL]
    assert clean_spec({'colors': ['#ff0000', '00ff00']})['layers'][0]['colors'] == ['ff0000', '00ff00'] and len(clean_spec({'layers': []})['layers']) == 1
    assert parse_fx({'fx': {'effect': 'Nope', 'speed': '250', 'pct': 'x'}}) == ('Cycle', 7, 40) and parse_fx({'fx': {'effect': 'Breathe', 'speed': 30, 'pct': 25}}) == ('Breathe', 30, 25)
    g = diy_groups(stripes); assert [c for c, _ in g] == [[255, 0, 0], [0, 255, 0], [0, 0, 255]] and sorted(i for _, l in g for i in l) == ALL
    fx = DiyFx(stripes, ('Cycle', 50, 40)); h = fx.per(fx.CYCLE); a, b = fx.frame(0.3, 0.05), fx.frame(h + 0.3, 0.05)
    assert a != b and all(b[ALL[i + 3]] == a[ALL[i]] for i in range(6)), 'Cycle must hand each run the previous colour'
    print('simulate: all effects OK (config %s, %d LEDs, %d pixels, zones: %s)' % (_cfg_path, N, N_DEV, ', '.join(ZONES)))

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--simulate': simulate(float(sys.argv[2]) if len(sys.argv) > 2 else 30); sys.exit()
    e = Engine()
    if len(sys.argv) > 1:
        mode = sys.argv[1]; secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20
        if mode not in MODES: sys.exit('modes: ' + ', '.join(MODES))
        e.start(mode); end = time.time() + secs
        try:
            while time.time() < end:
                t = time.time(); fps = e.run_frame()
                if mode == 'LED Sweep': print('\rLED', e.eff.led, end='', flush=True)
                time.sleep(max(0.0, 1 / fps - (time.time() - t)))
        finally: print(); e.stop()
    else:
        try: e.serve()
        except KeyboardInterrupt: e.stop()
