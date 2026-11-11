"""Turns config.json 'layout' into govee_led_layout.json, the drawing govee-rt.html paints the live colours on.

Each layout run places LEDs [first, last] evenly along a straight line from 'from' to 'to' (canvas pixels). Draw your
roofline as a handful of straight runs - corners, gable rakes, eaves. LEDs in no run are not drawn. No 'layout' at all
= one straight line, good enough to start.

    python layout.py                 # writes <www_dir>/govee_led_layout.json
    python layout.py --out file.json
"""
import sys, os, json

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = os.environ.get('GOVEE_CONFIG') or os.path.join(HERE, 'config.json')
CFG = json.load(open(_cfg if os.path.exists(_cfg) else os.path.join(HERE, 'config.example.json')))
N = int(CFG.get('leds', 80))

def build(cfg):
    lay = cfg.get('layout') or {'width': 1200, 'height': 200, 'runs': [{'leds': [0, N - 1], 'from': [40, 100], 'to': [1160, 100]}]}
    leds = [None] * N
    for r in lay['runs']:
        a, b = r['leds']; idx = list(range(a, b + 1)) if b >= a else list(range(a, b - 1, -1))
        (x0, y0), (x1, y1) = r['from'], r['to']
        for k, i in enumerate(idx):                          # centred in its slot, so neighbouring runs don't stack at the corner
            u = (k + 0.5) / len(idx); leds[i] = [round(x0 + (x1 - x0) * u, 1), round(y0 + (y1 - y0) * u, 1)]
    paths, cur = [], []
    for i, p in enumerate(leds):                             # outline = consecutive placed LEDs
        if p: cur.append(i)
        elif cur: paths.append(cur); cur = []
    if cur: paths.append(cur)
    return {'w': lay['width'], 'h': lay['height'], 'leds': leds, 'paths': paths, 'labels': lay.get('labels', [])}

if __name__ == '__main__':
    out = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else os.path.join(CFG.get('www_dir', HERE), 'govee_led_layout.json')
    L = build(CFG)
    assert len(L['leds']) == N and all(p is None or len(p) == 2 for p in L['leds'])
    json.dump(L, open(out, 'w'))
    print('wrote', out, '-', sum(p is not None for p in L['leds']), 'of', N, 'LEDs placed')
