"""Static per-LED Govee themes, uploaded straight to an H706C over LAN as a DIY scene (no app segments, no cloud).

Once uploaded the lights run the theme on their own - no PC needed afterwards. Home Assistant runs this file via
shell_command.govee_diy from script.govee_apply_theme (see ha/govee_package.yaml).

DIY upload format (reverse-engineered from a Govee app upload, verified byte for byte by the self-check below):
  a3 00 01 <n_packets> <payload...>   a3 01 ..   a3 ff <last>      (19 data bytes + XOR, 17 payload bytes per packet)
  payload = 03 <effect> <speed> <bg brightness %> <background R G B> <n_groups> | per group: <n_leds> R G B <led idx...>
  * Every LED not listed in a group shows the BACKGROUND colour. The Govee app uploads white (ff ff ff) there, which
    lights all the "gaps" white - use (0, 0, 0) for dark gaps. Listing the gaps as a black group does NOT work.
  * effect = one of the app's six per-LED ("finger sketch") effects, see EFFECTS; speed 0-100 (0x64 = fastest); then the
    background brightness %. Mapped by saving the same DIY in the app with one setting changed and diffing the uploads
    (govee2mqtt caches the app's Tap-to-Run commands). 03 02 07 05 = Cycle at the slowest speed = a static look.
  then 33 05 0a <id> 00 03 = apply DIY <id>; brightness 33 04 <pct> is sent first.
  * The strip IGNORES an upload that re-uses the DIY id it is already showing, so every theme gets its own id (0x80 + index).
LED indexes are physical, 0 = the power end (same numbering as the realtime engine).

    python govee_diy.py              # self-check + export govee_themes_px.json (sends nothing)
    python govee_diy.py "<theme>"    # self-check, export, then send the theme to the lights

Reads config.json next to this file (govee_ip, leds, corners, ha_www_dir). Every run exports THEMES as per-LED colours to
<ha_www_dir>/govee_themes_px.json, which govee-rt.html draws. Themes saved from the page's theme builder (kind "Controller")
arrive in <ha_www_dir>/govee_diy_themes.json (the engine writes it) and are applied as 'DIY: <name>'.
"""
import sys, os, json, socket, base64, time, functools, operator

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = os.path.join(HERE, 'config.json')
CFG = json.load(open(_cfg if os.path.exists(_cfg) else os.path.join(HERE, 'example_config.json')))
IP, N = CFG['govee_ip'], int(CFG.get('leds', 80))
WWW = CFG.get('ha_www_dir', '/config/www')
EFFECTS = {'Cycle': 0x02, 'Clockwise': 0x09, 'Counterclockwise': 0x0a, 'Twinkle': 0x0f, 'Gradient': 0x13, 'Breathe': 0x14}
def effect(name='Cycle', speed=7, bg_pct=5): return bytes([0x03, EFFECTS[name], max(0, min(100, int(speed))), max(0, min(100, int(bg_pct)))])
EFFECT = effect()                                              # 03 02 07 05: the static look
CORNERS = CFG.get('corners', [])

# Theme name -> (brightness %, [(rgb, [led indexes]), ...], background rgb[, effect(...)]). Add your own here AND to the
# `diy` list in script.govee_apply_theme. A name can match a Govee app scene name to replace that scene with a LAN-drawn one.
THEMES = {
    'Corners': (40, [((255, 198, 118), CORNERS)], (0, 0, 0)),                                     # warm white on the corner LEDs only
    'Halloween PG': (100, [((0, 255, 0), list(range(0, N, 4))), ((139, 0, 255), list(range(2, N, 4)))], (0, 0, 0)),   # green / purple, dark gaps
}
DIY_PATH = os.path.join(WWW, 'govee_diy_themes.json')          # from the theme builder: {name: {pct, fx: [effect, speed], groups: [[rgb, leds], ...], spec}}
try:
    for _n, _d in json.load(open(DIY_PATH)).items():
        THEMES['DIY: ' + _n] = (_d['pct'], [(tuple(rgb), leds) for rgb, leds in _d['groups']], (0, 0, 0), effect(*_d['fx']))
except FileNotFoundError: pass
except Exception as _e: print('govee_diy_themes.json skipped:', _e, file=sys.stderr)

# Captured from the Govee app (green every 4th LED, purple two over, white background, the app's 120-LED kit numbering).
CAPTURE = ['owABBQMCBwX///8CHgD/AAAECLQ=', 'owEMEBQYHCAkKCwwNDg8QERITK4=', 'owJQVFhcYGRobHB0HosA/wIGCsE=',
           'owMOEhYaHiImKi4yNjo+QkZKTq4=', 'o/9SVlpeYmZqbnJ2AAAAAAAAAFg=']

def frame(b): b = bytes(b).ljust(19, b'\0'); return base64.b64encode(b + bytes([functools.reduce(operator.xor, b)])).decode()

def packets(groups, bg=(0, 0, 0), fx=EFFECT):
    p = fx + bytes([*bg, len(groups)])
    for rgb, leds in groups: p += bytes([len(leds), *rgb, *leds])
    first, rest = p[:15], p[15:]
    chunks = [rest[i:i + 17] for i in range(0, len(rest), 17)]
    out = [bytes([0xa3, 0x00, 0x01, 1 + len(chunks)]) + first]
    out += [bytes([0xa3, i + 1 if i < len(chunks) - 1 else 0xff]) + c for i, c in enumerate(chunks)]
    return out

def export():
    out = {}
    for name, (pct, groups, bg, *_) in THEMES.items():
        px = [bg] * N
        for rgb, leds in groups:
            for i in leds:
                if i < N: px[i] = rgb
        out[name] = {'pct': pct, 'px': ''.join('%02x%02x%02x' % c for c in px)}
    if os.path.isdir(WWW):
        with open(os.path.join(WWW, 'govee_themes_px.json'), 'w') as f: json.dump(out, f)
    return out

def apply(name):
    pct, groups, bg, *fx = THEMES[name]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def pt(frames): s.sendto(json.dumps({"msg": {"cmd": "ptReal", "data": {"command": [frame(f) for f in frames]}}}).encode(), (IP, 4003))
    pt([bytes([0x33, 0x04, pct])]); time.sleep(0.3)
    did = 0x80 + list(THEMES).index(name)                      # own id per theme: a re-used id is ignored by the strip
    pt(packets(groups, bg, *fx) + [bytes([0x33, 0x05, 0x0a, did, 0x00, 0x03])])

if __name__ == '__main__':
    # self-check: the encoder must reproduce the app's captured upload byte for byte
    kit = [((0, 255, 0), list(range(0, 120, 4))), ((139, 0, 255), list(range(2, 120, 4)))]
    assert [frame(f) for f in packets(kit, bg=(255, 255, 255))] == CAPTURE, 'encoder does not match the captured upload'
    assert effect() == EFFECT and effect('Breathe') == bytes.fromhex('03 14 07 05') and effect('Twinkle', 100) == bytes.fromhex('03 0f 64 05')
    assert packets([((255, 0, 0), [0, 1])], (0, 0, 0), effect('Breathe', 50))[0][4:8] == bytes.fromhex('03 14 32 05'), 'effect bytes must lead the payload'
    px = export()['Corners']['px']
    assert [i for i in range(N) if px[i * 6:i * 6 + 6] != '000000'] == sorted(CORNERS), 'export lost the corners'
    if len(sys.argv) > 1: apply(sys.argv[1]); print('sent', sys.argv[1])
    else: print('self-check OK, themes:', ', '.join(THEMES))
