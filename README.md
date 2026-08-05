# Atios SmartCore — Home Assistant integration

Local-push integration for the [Atios SmartCore](https://atios.ch/products/smartcore)
DALI-2 controller. Talks **raw DALI** over the SmartCore's emulated Lunatone
websocket (`daliFrame` / `daliAnswer` / `daliMonitor`) using the
[`lunatone-dali2-iot`](https://pypi.org/project/lunatone-dali2-iot/) library,
with a fallback to the native `POST /api/dali/iface` HTTP endpoint.

> The SmartCore does **not** expose the Lunatone REST `/devices` API, so the
> official `lunatone` core integration cannot drive it. This integration works
> at the DALI frame level instead.

## Status

| Piece | State |
|---|---|
| DALI frame encode/decode (`dali.py`) | ✅ verified against reference frames (`FF 10` goto-scene, `01 91` query-gear-present) |
| WS transport + reconnect + HTTP fallback (`hub.py`) | ✅ written against the documented library API; needs a device to confirm the emulated stack behaves identically |
| Broadcast light + brightness/on/off | ✅ first cut |
| Per-address lights | ✅ configurable via options flow (confirmed on device: LED controller at A0) |
| QUERY status read | ✅ HTTP answer shape confirmed on device (`{success,bus_busy,collision_detected,data}`) |
| `send_dali_frame` / `recall_scene` services | ✅ |
| DALI-2 event decode (`dali.py`) | ✅ 62386-103/-301, verified byte-for-byte vs python-dali on 585 frames |
| Button events (`event.py`) | ✅ buttons auto-appear on first press with named gestures (Device scheme); Device/Instance scheme emits raw event_info |
| Zeroconf discovery | 🟨 zeroconf flow built (matches Lunatone-emulation `type=dali-2-*` + name `atios*`/`smartcore*`); confirm the SmartCore's actual mDNS/TXT on device and tighten the manifest |
| Firmware `update` entity | ✅ installed version from `/ota_status` (2.7.5, serial exposed); latest-version source still needed for update *notifications* |
| Web-UI iframe panel | ✅ sidebar panel via options flow; probes for X-Frame-Options / CSP / mixed-content and warns |

## Install (HACS custom repo)

Add this repo as a custom repository (type: Integration), install, restart,
then **Settings → Devices & Services → Add → Atios SmartCore** and enter the IP.

To show the SmartCore web UI in the sidebar: **Settings → Devices & Services → Atios SmartCore → Configure**. If the panel is blank, check the HA log — the integration probes for `X-Frame-Options`/CSP/mixed-content and logs the exact reason.

## Calibration steps once the device is in hand

1. **Confirm the WS endpoint** — start HA with debug logging for `custom_components.atios`;
   confirm `websocket connected`. If not, the native HTTP path still drives lights.
2. **Verify QUERY answers** — call `atios.send_dali_frame` with `data: [1, 160]`
   (`QUERY ACTUAL LEVEL` to address 0), `wait_for_answer: true`, and check the
   returned byte matches the fixture level.
3. **Confirm buttons** — run `python3 tools/monitor.py <smartcore-ip>` and press
   each Merten push (single / double / long). Device-scheme buttons decode
   directly to named gestures; if any print `gesture=?` (Device/Instance
   scheme), note the `info=` value and confirm it against `PUSHBUTTON_EVENTS`.
   In HA, each button auto-appears as an `event` entity on first press.
4. **Wire real lights** — replace the broadcast-only list in `light.py` with the
   discovered short addresses (or add an options flow).
5. **mDNS** — the zeroconf flow is already wired. Confirm the SmartCore's real
   advertisement with `avahi-browse -rt _http._tcp.local` (also try `_dali._tcp`,
   `_atios._tcp`). Check the service type, the instance name, and the TXT keys
   (`uid`, `serial`, `device`, `manufacturer`, `type`). If it differs from the
   three match entries in `manifest.json`, adjust them; the `async_step_zeroconf`
   handler already tolerates missing/renamed TXT keys and falls back to the host.

## Bounty deliverables (Atios) — mapping

1. Discover in network → 🟨 zeroconf flow (verify service/TXT on device, step 5)
2. Show web interface in HA → ✅ iframe sidebar panel (Settings → Devices → Atios → Configure)
3. Firmware update notifications → 🟨 installed version done via /ota_status; latest-version source pending
4. Send/receive custom DALI packets → ✅ `send_dali_frame` + monitor stream
