# Atios SmartCore — Home Assistant integration

Local, push-based integration for the [Atios SmartCore](https://atios.ch/products/smartcore)
DALI-2 controller. It talks to the SmartCore directly over your LAN — native
`aiohttp`, no cloud, no external Python dependencies: raw DALI frames via the
HTTP `POST /api/dali/iface` endpoint, firmware status via `/ota_status`, and a
`ws://<host>/ws` monitor stream for DALI-2 input events. Confirmed on SmartCore
firmware 2.7.5.

## Basic vs. advanced — which path should I use?

The SmartCore already holds a full device model internally (named devices,
groups, relative dimming, relay outputs, binary inputs) and exposes it
over **Matter**. For everyday use that is the recommended path, and it does
**not** require this integration.

**Basic (recommended for most users):**

1. Scan the DALI bus in the SmartCore **DALI Configurator**.
2. Build your Matter devices in the **Accessory Manager** — you combine the
   inputs and outputs the way they are actually wired, and each combination
   becomes one Matter accessory. A push-button input plus a relay output becomes
   a Matter generic switch; a DALI-2 push button plus a DALI light (single
   address or group) becomes a Matter dimmable light.
3. Pair the SmartCore into Home Assistant via **Matter**.

Home Assistant then shows the curated model natively — turning a group on keeps
its member addresses in sync, relative dimming works, and the relay outputs and
binary inputs are available, because all of that logic stays in the SmartCore
firmware where it belongs.

**Scenes are not configured in the SmartCore.** You create them in whichever
Matter app you use — Apple Home, Home Assistant, Google Home — on top of the
accessories the SmartCore exposes. *DALI scenes* are a completely different
thing (scene levels stored in the gear itself, recalled over the bus); firmware
support for those is planned, and until then you can drive them today with the
advanced path below by sending the raw DALI packets yourself.

**Advanced (this integration):** direct low-level access for power users — send
and receive raw DALI packets, recall scenes, watch the bus monitor, the firmware
update entity, and the SmartCore web UI as a sidebar panel. It also lets you
reach devices the SmartCore does not model yet — DALI-2 sensors and proprietary
DALI devices can be driven from Home Assistant through raw frames even while the
firmware has no native support for them. Use it when you want raw DALI control
or would rather not run Matter. It works standalone and also sits happily
alongside a Matter-paired SmartCore. Several of its entities and services are
low-level and are described as *advanced* below.

## Status

| Piece | State |
|---|---|
| DALI frame encode/decode (`dali.py`) | ✅ verified against reference frames (`FF 10` goto-scene, `01 91` query-gear-present) |
| Transport (`hub.py`) | ✅ native aiohttp; HTTP `/api/dali/iface` confirmed on device, `ws://<host>/ws` monitor connects (101) |
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

### Branding

Brand images ship inside the integration (`custom_components/atios/brand/`) and
are served by Home Assistant's local brands proxy (HA 2026.3+), so the SmartCore
icon and logo appear in the UI with no CDN round-trip and no separate
`home-assistant/brands` submission. Artwork provided by Atios AG.

### Basic / Advanced mode

**Settings → Devices & Services → Atios SmartCore → Configure** has a **Mode**
switch:

- **Basic** (default) — named per-address lights and the firmware update entity
  only. Pair the SmartCore via Matter for the full curated model (groups,
  relays, inputs).
- **Advanced** — additionally exposes the bus-wide **All lights** (broadcast)
  entity and other raw-DALI features. The `atios.send_dali_frame` /
  `atios.recall_scene` services are available in either mode for power users.

Changing the mode reloads the integration and re-creates entities accordingly.

## Contributing

Developer notes and the hardware bring-up checklist live in
[CONTRIBUTING.md](CONTRIBUTING.md).
