#!/usr/bin/with-contenv sh
# Everything you own lives in /homeassistant/govee_rt (= the govee_rt folder in your HA config): config.json, knobs, designs.
D=/homeassistant/govee_rt
export GOVEE_WWW=/homeassistant/www GOVEE_DATA=$D GOVEE_HA_CONFIG=/homeassistant
mkdir -p "$D" "$GOVEE_WWW"

# Your own engine copy wins: drop a customised govee_rt_engine.py into the folder and restart the add-on.
if [ -f "$D/govee_rt_engine.py" ]; then
    echo "Running your engine: $D/govee_rt_engine.py"
    cd "$D" && exec python3 -u govee_rt_engine.py
fi

if [ ! -f "$D/config.json" ]; then
    cp /app/config.example.json "$D/config.json"
    echo "Wrote $D/config.json - set govee_ip, leds and your zones, then restart the add-on."
    exit 1
fi
export GOVEE_CONFIG="$D/config.json"
[ -f "$GOVEE_WWW/govee-rt.html" ] || cp /app/govee-rt.html "$GOVEE_WWW/"
python3 /app/layout.py --out "$GOVEE_WWW/govee_led_layout.json"
cd /app && exec python3 -u govee_rt_engine.py
