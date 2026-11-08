# ISDT 608PD Bluetooth monitor

Read live charging data from an **ISDT 608PD** charger over Bluetooth LE, without the ISD Link app,
and get alerted when a battery is charged. Includes:

- **A documented BLE protocol** (below). As far as I know it isn't published anywhere else.
- **A desktop app** (Python, Windows) with a live progress bar, time-to-full estimate, per-cell voltages and IR,
  and alerts by Windows notification, alarm, email or Home Assistant push.
- **An ESP32 bridge** (`esp32/`) that runs 24/7 and publishes everything to Home Assistant, including a phone
  notification when a charge finishes.
- **A Home Assistant dashboard** (`homeassistant/dashboard.yaml`).

Tested with a 608PD running firmware v1.10. Other ISDT chargers that use the ISD Link app probably use the
same framing with different commands.

> Not affiliated with or endorsed by ISDT. The protocol was worked out independently for interoperability.
> This repository contains none of ISDT's code.

The charger accepts **one** BLE connection at a time. Close ISD Link (or turn off Bluetooth on your phone)
while using any of this.

## Desktop app
```
pip install -r requirements.txt
python isdt_gui.py          # GUI
python isdt_monitor.py      # terminal version (--email, --phone, --test-alert)
```
| File | |
|---|---|
| `isdt.py` | BLE protocol: bind, real-time polling, packet parser |
| `estimator.py` | time-to-full estimate (see below) |
| `notify.py` | Windows notification, alarm, email (SMTP app password), Home Assistant push |
| `ha_live.py` | mirrors readings to Home Assistant as `sensor.isdt_charger_*` entities |
| `isdt_gui.py`, `isdt_monitor.py` | GUI and CLI |

Settings are made in the GUI. Passwords and tokens are stored in Windows Credential Manager. Local files
the app creates (`app_id.txt`, `*_config.json`, `estimator_history.json`) are personal and not tracked.

**Email:** *Email settings...*. Use an app password (for Gmail: myaccount.google.com/apppasswords).

**Home Assistant:** *Home Assistant...*. Enter your URL and a long-lived access token (Profile → Security), then
press *Find* to pick your phone's `notify.mobile_app_*` service. The app also publishes live entities.
Untick *Show live charger data* if you only want the notification.

## ESP32 bridge
See [`esp32/README.md`](esp32/README.md). Copy `secrets.example.h` to `secrets.h`, fill it in and flash.
Wi-Fi networks can also be added later from its web page or setup hotspot.

## Home Assistant entities and dashboard
Both the desktop app and the ESP32 publish the same entities through the REST API (`POST /api/states/...`),
so no custom integration is needed:

`sensor.isdt_charger_status` (attributes `pack`, `phase`, `status_code`), `_battery` (%), `_time_left` (min),
`_done_at` (timestamp), `_pack_voltage`, `_current`, `_power`, `_added` (mAh), `_elapsed` (min), `_input_voltage`,
`_cell_1` … `_cell_6` (attribute `ir_mohm`), and `binary_sensor.isdt_charger_connected`.

Entities set this way disappear when Home Assistant restarts and come back on the next update. For the
dashboard, paste `homeassistant/dashboard.yaml` into a new view (Edit dashboard → add view → Edit in YAML).
The status card needs [Mushroom](https://github.com/piitaya/lovelace-mushroom).

## Time-to-full estimate
Lithium packs charge in two phases. The charger's % jumps to ~99 when the second phase starts and then
stays there, so % alone is a poor guide near the end.
1. **Constant current (CC).** A least-squares fit of % against time (last 10 min) predicts when CV starts,
   then the expected CV duration is added.
2. **Constant voltage (CV).** The current decays roughly exponentially. A fit of ln(current) against time
   (last 4 min) gives the time until the current reaches the charger's cut-off.
3. **Learning.** After each charge, the CV duration and cut-off current (as a fraction of the CC current)
   are learned per chemistry and cell count.

## Protocol
GATT services on the 608PD (advertised name contains `608PD`, service `AF00`, manufacturer data `0xABBA`):

| Service | Characteristic | Properties | Use |
|---|---|---|---|
| `AF00` | `AF01` | write without response, notify | commands and responses |
| `AF00` | `AF02` | write without response, notify | binding |
| `DB00` | `DB01` | notify | plain-text debug status, about 1/s (see below) |
| `FFE0` | `FFE1`…`FFE5` | various | not needed |

### 1. Bind (required)
Without binding, the charger drops the connection after about 6 s.
```
write AF02:   18  <16-byte app id>  00  <mode>     mode 0 first; if refused retry with 1
notify AF02:  19  <result>                          result 00 = accepted
```
The app id is any random 16 bytes. Keep it stable per client (the official app stores a random UUID per device).

### 2. Real-time data
Poll about once a second:
```
write AF01:   12 E4
notify AF01:  31 E5  <payload>
```
Payload, little-endian:

| Offset | Size | Field |
|---|---|---|
| 2 | u16 | input voltage, mV |
| 4 | u16 | output power, W |
| 6 | u8 | status: 0 standby, 1 charging, **2 charge done**, 3 discharging, 4 discharge done |
| 7 | u8 | chemistry: 0 LiHV, 1 LiPo, 2 LiIon, 3 LiFe, 4 PbAc, 5 NiMH, 6 ULiHV |
| 8 | u16 | current, mA |
| 10 | u16 | pack voltage, mV |
| 12 | u8 | charge, % |
| 13 | u8 | balance port connected (0 = no) |
| 14 | u32 | elapsed, s |
| 18 | u32 | charged capacity, mAh |
| 22 | u8 | cell count (only if balance port ≠ 0) |
| 23 + 4n | u16 + u16 | cell n: voltage mV, internal resistance in 0.1 mΩ |

Example (LiHV 4S, charging, 85 %, 16.709 V, 1.14 A, 390 mAh after 20:32):
```
31 e5 2d 4e 15 00 01 00 74 04 45 41 55 03 d0 04 00 00 86 01 00 00 04 47 10 db 00 45 10 f0 00 4b 10 ea 00 4a 10 de 00
```

### Other commands seen (not used here)
`12 9E` charger settings query, `12 9C …` settings write, `12 EC` preset tasks query,
`12 EA …` start a charge task, `12 E6` stop task. Write commands change charger state, so handle them with care.

### Debug stream on DB01
Without binding, `DB01` pushes a text report about once a second, e.g.
```
On:1, PDM:1 PDV:20000 PDC:2250
Con=3 CV=4S:3870, 3871, 3869, 3868, 1578, 1561
InV:20013, OutV:15505 v1.10
M:1, Sta:1 VBUS:0/0 VBAT:18400/1200 VP:0 E:00
S:3 E:0[None] L:0[None]
IBUS:2137 IBAT:1200
```
It is handy for exploring, but use the binary protocol above for real data.

## Licence
MIT. See [LICENSE](LICENSE).
