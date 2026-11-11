"""Add-on options (/data/options.json, the Configuration tab) -> config.json for the engine, layout.py and govee_diy.py.

Ranges are typed as "13-28" (first-last, either direction, 0-based from the power end); single LEDs as "40".
An optional layout.json next to the output (the roof drawing, see example_config.json 'layout') is merged in.

    python3 options_to_config.py /data/options.json /homeassistant/govee_rt/config.json
"""
import sys, os, json

def rng(s):
    a, _, b = str(s).replace(' ', '').partition('-')
    return [int(a), int(b or a)]

def leds(items):
    out = []
    for s in items:
        a, b = rng(s); out += list(range(a, b + 1)) if b >= a else list(range(a, b - 1, -1))
    return out

def build(o, layout=None):
    zones = {'arches': [rng(s) for s in o.get('arches', [])], 'runways': [rng(s) for s in o.get('runways', [])],
             'sparkle': [rng(s) for s in o.get('sparkle', [])], 'off': leds(o.get('always_off', []))}
    if o.get('door'): zones['door'] = rng(o['door'])
    cfg = {'_comment': 'Generated from the add-on Configuration tab on every start - edit it there, not here.',
           'govee_ip': o['govee_ip'], 'leds': o.get('leds', 80), 'max_pixels': o.get('max_pixels') or o.get('leds', 80),
           'light_entity': o.get('light_entity') or 'light.permanent_lights_pro', 'ha_www_dir': '/config/www',
           'zones': {k: v for k, v in zones.items() if v}, 'corners': leds(o.get('corners', []))}
    if o.get('default_theme'): cfg['default_theme'] = o['default_theme']
    if layout: cfg['layout'] = layout
    return cfg

if __name__ == '__main__':
    if len(sys.argv) < 3:   # self-check
        c = build({'govee_ip': '1.2.3.4', 'leds': 80, 'max_pixels': 0, 'arches': ['13-28', '51-60'], 'runways': ['29-39', '50-45'],
                   'door': '40-44', 'sparkle': ['0-12'], 'always_off': ['69'], 'corners': ['0', '12-13']})
        assert c['zones'] == {'arches': [[13, 28], [51, 60]], 'runways': [[29, 39], [50, 45]], 'sparkle': [[0, 12]], 'off': [69], 'door': [40, 44]}, c
        assert c['corners'] == [0, 12, 13] and c['max_pixels'] == 80 and 'default_theme' not in c
        print('ok'); sys.exit()
    src, dst = sys.argv[1], sys.argv[2]
    lay = os.path.join(os.path.dirname(dst), 'layout.json')
    cfg = build(json.load(open(src)), json.load(open(lay)) if os.path.exists(lay) else None)
    json.dump(cfg, open(dst, 'w'), indent=1)
