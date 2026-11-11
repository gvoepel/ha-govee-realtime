#!/usr/bin/with-contenv sh
# Settings come from the Configuration tab (/data/options.json). Knobs, saved designs and the optional layout.json live in
# /homeassistant/govee_rt (= the govee_rt folder in your HA config).
D=/homeassistant/govee_rt
export GOVEE_WWW=/homeassistant/www GOVEE_DATA=$D GOVEE_HA_CONFIG=/homeassistant
mkdir -p "$D" "$GOVEE_WWW"
GOVEE_IP=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('govee_ip', ''))")
if [ -z "$GOVEE_IP" ]; then
    echo "Set govee_ip (the light's IP address) on the Configuration tab, then start the add-on again."
    exit 1
fi
export GOVEE_IP

# Your own engine copy wins: drop a customised govee_rt_engine.py into the folder and restart the add-on.
if [ -f "$D/govee_rt_engine.py" ]; then
    echo "Running your engine: $D/govee_rt_engine.py"
    cd "$D" && exec python3 -u govee_rt_engine.py
fi

python3 /app/options_to_config.py /data/options.json "$D/config.json" || exit 1
export GOVEE_CONFIG="$D/config.json"
[ -f "$GOVEE_WWW/govee-rt.html" ] || cp /app/govee-rt.html "$GOVEE_WWW/"
python3 /app/layout.py --out "$GOVEE_WWW/govee_led_layout.json"
cd /app && exec python3 -u govee_rt_engine.py
