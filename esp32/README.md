# ISDT 608PD → Home Assistant on an ESP32

An always-on bridge that does what the desktop app does, without the PC:
- connects to the charger over BLE (bind and real-time polling, same protocol as `isdt.py`)
- estimates time to full (port of `estimator.py`; learned values are kept in flash)
- publishes the same `sensor.isdt_charger_*` entities, so the **Charger** dashboard tab works unchanged
- sends the "Battery charged" phone notification through Home Assistant

Only one device can talk to the charger at a time. Close the ISD Link app and don't run the desktop app while the ESP32 is on.

## Files
| File | |
|---|---|
| `isdt_charger/isdt_charger.ino` | main sketch |
| `charger.h` / `estimator.h` / `ha_client.h` / `net.h` | BLE, time-left estimate, Home Assistant, Wi-Fi and web page |
| `secrets.h` | **your** Wi-Fi list, Home Assistant URL/token, phone notify service and setup password. Not in the repo: copy `secrets.example.h` and fill it in. |
| `secrets.example.h` | template without secrets |
| `ca_certs.h` | Let's Encrypt root certificates for HTTPS to Home Assistant |
| `build/` | compiled firmware (created when you build; not in the repo, it contains your secrets) |

## Home Assistant settings
In `secrets.h`: `HA_HOST`/`HA_PORT`/`HA_USE_TLS` for your instance, `HA_TOKEN` (a long-lived access token
from Profile → Security), and `HA_NOTIFY_SERVICE`, your phone's companion-app service without the `notify.`
prefix, e.g. `mobile_app_pixel_8` (see Developer tools → Actions). `ca_certs.h` has the Let's Encrypt roots,
which covers DuckDNS/Let's Encrypt and Nabu Casa URLs. For a plain local `http://` URL set `HA_USE_TLS 0`.

## Wi-Fi
Either add networks to `WIFI_NETWORKS` in `secrets.h` before building, or add them later without reflashing:
- **On Wi-Fi:** open `http://isdt-charger.local` (user `admin`, password = `SETUP_PASSWORD` in `secrets.h`).
- **No known network in range:** after 1 minute the ESP32 opens a hotspot called `ISDT-Charger-Setup`
  (same password). Join it from your phone, open `http://192.168.4.1` and add your network.

Up to 10 networks can be added this way. It always joins the strongest known network in range.

## Build and flash
Board: any ESP32 with Bluetooth LE, e.g. *ESP32 Dev Module*. It needs the `NimBLE-Arduino` library.
```
arduino-cli compile --fqbn "esp32:esp32:esp32:PartitionScheme=min_spiffs" --output-dir build isdt_charger
arduino-cli upload  --fqbn "esp32:esp32:esp32:PartitionScheme=min_spiffs" -p COM5 --input-dir build isdt_charger
```
Or write `build/isdt_charger.ino.merged.bin` at offset `0x0` with esptool or the
[web flasher](https://espressif.github.io/esptool-js/). In the Arduino IDE, pick the partition scheme
*Minimal SPIFFS (1.9MB APP with OTA)*.

**Updating later over Wi-Fi (OTA):** the ESP32 appears as the network port `isdt-charger` in the Arduino IDE.
The password is `SETUP_PASSWORD`.

For other ESP32 variants (S3, C3, C6) change the FQBN, e.g. `esp32:esp32:esp32s3`. The ESP32-S2 has no Bluetooth.

## Status
- Serial monitor (115200 baud) logs Wi-Fi, charger connection and a reading every 10 s.
- The web page shows live values, any Home Assistant error, and the Wi-Fi list.
- In Home Assistant every entity has the attribute `source: esp32`.

If Home Assistant ever rejects the token (HTTP 401/403), the ESP32 stops sending until it is restarted,
so it can't trigger Home Assistant's IP ban.
