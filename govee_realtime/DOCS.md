# Govee Realtime

Paints per-LED animations on a Govee **Permanent Lights Pro (H706C)** over your LAN (the strip's realtime protocol, UDP
4003), at 20-40 fps, and follows Home Assistant helpers so effects can be picked from dashboards and automations.
It also renders the theme builder's designs and previews for the control page.

> Tested on one H706C on one house. Other Govee models may use different LAN commands.

## Before you start

- **LAN Control** on for the light in the Govee app (device settings).
- In the app's **Settings of Number of Segments**, set the strip's real length: segments = lights / 10, cut = Yes, and the
  number of red lights = the lights in the last segment. With the nominal (uncut) length the strip drives realtime
  pixels in pairs and rejects frames as long as your real light count.
- The Home Assistant helpers in `engine/govee_package.yaml` from this repository (copy it into `/config/packages/`):
  `input_select.govee_rt_scene`, `input_text.govee_rt_cmd`, `script.govee_apply_theme` and friends.
  Static / controller themes also need `engine/govee_diy.py` copied to `/config/govee_rt/` (the package's shell command
  runs it there, with the `config.json` the add-on writes).

## Setup

1. On the **Configuration** tab set the light's IP address and number of lights. Zones are optional: type ranges like
   `13-28` (0-based LED indexes from the power end). Effects fall back to the whole strip when a zone is missing.
2. Start the add-on. The control page is at `/local/govee-rt.html`.

On every start the settings are written to `/config/govee_rt/config.json` (don't edit it, it is overwritten). Knobs and
saved designs are kept in the same folder. To draw your roofline on the page, put a `layout.json` there with the
`layout` block from `engine/example_config.json` (runs of LEDs from/to canvas points) and restart.

## Your own engine

If `/config/govee_rt/govee_rt_engine.py` exists, the add-on runs that file instead of the bundled one, from that folder.
Use it for a heavily customised engine; it gets `SUPERVISOR_TOKEN` (talk to `http://supervisor/core/api` and
`ws://supervisor/core/websocket`), `GOVEE_IP` (from the Configuration tab) and `GOVEE_HA_CONFIG=/homeassistant` in its environment.

## Only one engine

Run the engine in one place only. Two engines sending frames to the same strip fight over it.
