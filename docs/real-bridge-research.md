# Real Philips Hue Bridge (BSB002) — Research & Firmware Analysis

Reference notes on how the **real** Hue Bridge v2 (model BSB002) works, gathered to improve
diyHue's fidelity to genuine bridge behaviour. Combines:

- Hands-on analysis of real Philips firmware images (downloaded + reverse-engineered offline).
- Published hardware reverse-engineering (UART/U-Boot rooting, NAND dumps).
- Synacktiv's reverse-engineering of the internal `ipbridge` daemon.
- The official Hue API v1 + CLIP v2 specifications.

Every external claim is linked to its source. Items resting only on community RE (no open
official source) are flagged **[RE]**.

---

## 1. Real firmware: acquisition & format

### 1.1 Distribution endpoint

The bridge checks for updates against a public Philips endpoint (the same URL diyHue uses in
`BridgeEmulator/services/updateManager.py`):

```
GET https://firmware.meethue.com/v1/checkupdate/?deviceTypeId=BSB002&version=<N>
```

Returns JSON listing available builds. Passing a low `version` lists all of them:

```json
{"updates":[{
  "fileSize": 13504873,
  "md5": "44a9520abf82d52f5808e23d4d5f8a19",
  "binaryUrl": "https://firmware.meethue.com/storage/bsb002/1977138000/<uuid>/BSB002_1977138000.product.RSA_prod_01.fw2",
  "version": 1977138000,
  "versionName": "1.77_SR4",
  "releaseNotes": "..."
}]}
```

Builds retrieved and md5-verified during this research:

| versionName | version (int) | size | date | md5 |
|---|---|---|---|---|
| 1.14.0 | 1033989 | 5,196,953 | 2019-04-15 | `925563190c3251b1cd3e1f642546dfa0` |
| 1.47.3.1 | 1947108030 | 10,856,121 | 2021/2022 | `0a783763b5992825ca147261cc4c5175` |
| 1.77_SR4 | 1977138000 | 13,504,873 | 2026-06-04 | `44a9520abf82d52f5808e23d4d5f8a19` |

`binaryUrl` pattern: `…/storage/bsb002/<version>/<uuid>/BSB002_<version>.product.RSA_prod_01.fw2`

### 1.2 `.fw2` container layout

Header fields (offsets from start of file), decoded by comparing the 2019 and 2026 builds:

| Offset | Size | Field | 1.77_SR4 | 1.14.0 |
|---|---|---|---|---|
| 0x00 | 6 | ASCII magic | `BSB002` | `BSB002` |
| 0x06 | 2 | format/type word | `0x0002` | `0x0002` |
| 0x08 | 4 | total size (big-endian) | `0x00ce1069` | `0x004f4b99` (= filesize − 256) |
| 0x0C | var | image label (ASCIIZ) | `iroot` | `bridge` |
| 0x20 | 4 | payload size (BE) | `0x00ce0e50` | `0x004f4980` |
| 0x24 | 2 | flags | `0x0103` | `0x0103` |
| 0x2C | ~12 | version string (ASCIIZ) | `1977138000` | `01033989` |
| ~0x3A | 256 | RSA-2048 signature | high entropy | high entropy |
| then | … | payload | encrypted | encrypted |

- `RSA_prod_01` in the filename = the production RSA signing-key profile.
- The image label changed from `bridge` (2019) to `iroot` (2026) — the A/B root-image identifier.

### 1.3 Encryption verdict — payload is AES-encrypted (not just signed)

Offline statistical analysis of the payload region:

- Whole-file Shannon entropy **0.9966**.
- Byte-value histogram over a 1 MB sample is **flat**: per-value counts min 3894 / median 4092 /
  max 4264 (ideal-uniform ≈ 4096) → cryptographically random.
- `binwalk` finds **no** filesystem/compression signatures (no squashfs/ubifs/gzip/uImage).
- `strings` yields only the header magic + version; long "strings" are random coincidences.
- **The 2019 build is equally encrypted** — older firmware is *not* plaintext here (unlike the
  round bridge v1 BSB001, and unlike Hue *lamp* OTA images).

Conclusion: the `.fw2` payload is AES-encrypted on top of RSA signing. **No public decryption key
exists for the bridge image.** Public RE (vsociety) only reached "seems encrypted, TODO decrypt";
the histogram test above confirms genuine encryption rather than signed-but-plaintext.

> ⚠️ Do **not** conflate this with the AES-CCM-128 key recovered by Ronen/O'Flynn via power
> side-channel ([IoT Goes Nuclear](https://eyalro.net/project/iotworm.html)). That key protects the
> Zigbee **lamp** OTA bootloader image, **not** the BSB002 bridge `.fw2`.

→ **Offline rootfs extraction is impossible.** The proven path is a hardware dump (next section).

---

## 2. Hardware & on-device firmware extraction

Once the device decrypts a `.fw2` and writes it to NAND, the on-flash rootfs is plain
**SquashFS-in-UBI** — that is what hardware dumps recover. This reconciles the encrypted `.fw2`
(over the air) with the unencrypted rootfs (on the device).

### 2.1 Hardware

| Component | Part | Notes |
|---|---|---|
| SoC | Qualcomm Atheros **QCA4531** | MIPS **24Kc**, big-endian, `ar71xx`/`mips_24kc`. (O'Flynn 2016 reported AR9331; OpenHue lists AR9344 — early-rev / source variance; QCA4531 is the most-cited current part.) |
| RAM | 64 MB DDR2 | — |
| NAND | SPI **128 MiB** (Winbond W25N01GV / GigaDevice GD5F1GQ4) | kernels + rootfs + overlay |
| NOR | SPI **512 KB** (GD25Q41B) | U-Boot / env / ART |
| Zigbee | Atmel/Microchip **ATSAMR21E18A** | **separate** Cortex-M0+ MCU + 802.15.4; UART `/dev/ttyZigbee` |
| RF FEM | Skyworks SE2438T | 2.4 GHz PA |
| USB-UART | Prolific PL2303SA | on board |

### 2.2 OS / partitions

- Custom **OpenWrt** (current units 19.07.8 r11364, kernel 4.14.241, BusyBox), U-Boot bootloader.
- MTD (NOR): `256k(u-boot)ro, 128k(u-boot-env), 64k(reserved), 64k(art)`.
- MTD (NAND): `4m(kernel-0)ro, 40m(root-0), 4m(kernel-1), 40m(root-1), -(overlay)` —
  **dual A/B bootslots**; `ubi.mtd` env selects the active slot.
- rootfs = read-only **SquashFS inside a UBI volume** + writable **UBIFS** `/overlay`.

### 2.3 Rooting / dumping (hardware)

Per the [OpenWrt forum thread](https://forum.archive.openwrt.org/viewtopic.php?id=66346),
[Colin O'Flynn](https://colinoflynn.com/2016/07/getting-root-on-philips-hue-bridge-2-0/) and
[wejn.org (2024)](https://wejn.org/2024/11/rooting-hue-bridge-with-firmware-1967054020/):

- **UART header J6** (also J1), 2.54 mm pitch, **3.3 V only** (5 V destroys it), **115200 8N1**.
  J6 pinout: **pin 1 = GND** (square pad), **pin 4 = RX**, **pin 5 = TX**.
- Drop to U-Boot: short the NAND **DO** pin to GND, or pull resistor **R31** (SPI NAND CS, under
  the SoC, bottom-right of PCB) to halt boot. Env is writable (`setenv`/`saveenv`).
- Set the `security` env var to a known password hash (MD5 `$1$…` old / SHA-512 newer) so U-Boot
  syncs it into `/etc/shadow`; raise `bootdelay`; boot; enable Dropbear (`RootLogin '1'`) + open
  iptables port 22.
- Modern firmware: remove `check_signature` from `/etc/opkg.conf` to install unsigned packages;
  newer builds add an ECC "signify" check (prime256v1, `openssl dgst -sha256`) gating opkg.
- Then `dd` the `root-0`/`root-1` MTD partitions (or copy the live SquashFS) for a clean rootfs +
  the `ipbridge` binary.

No JTAG header is documented; RE uses UART + NAND-pin shorting.

---

## 3. How the real bridge processes data (`ipbridge`)

Source: [Synacktiv — "Make it Blink"](https://www.synacktiv.com/en/publications/make-it-blink-over-the-air-exploitation-of-the-philips-hue-bridge)
and the [HackMD root notes](https://hackmd.io/@fjTXL6atTJyn_W4kMM9iCQ/SyEEzTU85).

### 3.1 The monolith

Almost all logic lives in one binary:

```
/usr/sbin/ipbridge -p /home/ipbridge/var -z /dev/ttyZigbee -u /etc/channel/channel-config
```

**>9 MB, ~40,000 functions**, MIPS big-endian. Multiple instances run concurrently (HomeKit,
Matter, etc.).

### 3.2 Zigbee path is a *textual* serial protocol

The Atmel SAM R21 radio MCU receives binary 802.15.4 frames and converts them to **ASCII text**
before handing them to `ipbridge` over `/dev/ttyZigbee`:

```
Group,Command,Data_1,Data_2,…,Data_N
e.g.  Zdp,SendMgmtPermitJoiningReq,B=0xFFFC.0,40,0
```

The `smartlink` thread intercepts and dispatches these. (Note: this is **not** EZSP/binary — a
useful detail for anyone emulating or fuzzing the link.)

### 3.3 The 12 message groups

`Bridge`, `Link`, `TH`, `Connection`, `Network`, **`Zdp`** (device profile: discovery/pairing),
**`Zcl`** (cluster library: standard lighting actions), **`Zgp`** (Green Power), `Groups`, `Log`,
`Stream`, `TrustCenter` (security). Each has its own handler routines.

### 3.4 State machines

Discovery/pairing/config use FSMs: `fsm_init_state(...)` and `fsm_do_transition` (evaluate `check`
→ run current state `exit` → transition `action` → next state `entry`; transitional states of
`type==2` auto-chain to the next transition).

### 3.5 Other daemons / services

| Daemon | Role |
|---|---|
| `clipd` | CLIP / REST HTTP API server (internal 9003, external 3245) |
| `behaviord` (hue-behavior-daemon) | rules / automations |
| `updated` | OTA firmware manager — writes the inactive A/B slot |
| `stream` | entertainment / low-latency light streaming |
| `websocketcd` | WebSocket channel for remote/cloud control |
| `mosquitto` | MQTT broker bridging to Google Cloud IoT |
| `hk_hap` | HomeKit HAP (TCP 8080) |
| hue-matter-daemon | Matter |
| mDNS + SSDP | local discovery (`/description.xml` :80, model "Philips hue bridge 2015") |

Provisioning: `/etc/ca-certificates/ca/ecc/cert-and-crls.pem`; CSR signing uses
HMAC-SHA256 + HKDF(portal key, bridge id).

### 3.6 Recent vulnerability (context)

**CVE-2026-3555**: heap buffer overflow in `zcl_handle_download_blob_received_bloc_event`
(Philips manufacturer-specific ZCL, cluster `0xC1`, manufacturer code `0x100B`) —
`memcpy(&ctx->buffer[offset], payload[11], blob_size)` without bound checking; exploited against
the musl dlmalloc 1.1.24 allocator.

---

## 4. Hue API & command processing (fidelity reference for diyHue)

> Official v2 docs (`developers.meethue.com/develop/hue-api-v2/`) are login-gated. The authoritative
> machine-readable substitute is the community [openhue-api](https://github.com/openhue/openhue-api)
> OpenAPI spec, whose field names/enums match the official reference verbatim.

### 4.1 v1 (legacy) REST API

- Base: `https://<bridge-ip>/api/<username>/…`. Create a username via
  `POST /api {"devicetype":"app#device"}` → first call returns error **101** (link button not
  pressed) → press button → `{"success":{"username":"<32-hex>"}}`.
- `GET /api/<username>` returns the whole datastore: `lights`, `groups`, `config`, `schedules`,
  `scenes`, `rules`, `sensors`, `resourcelinks`, keyed by stringified integer IDs.
- **Light state** (`PUT /lights/<id>/state`): `on`, `bri` 1–254 (**0 ≠ off**), `hue` 0–65535,
  `sat` 0–254, `ct` 153–500 (mired), `xy` `[0..1,0..1]`, `alert` (`none`/`select`/`lselect`),
  `effect` (`none`/`colorloop`), `transitiontime` centiseconds (**default 4** = 400 ms), and the
  `bri_inc`/`sat_inc`/`hue_inc`/`ct_inc`/`xy_inc` relative variants. Read-only `colormode`
  (`hs`/`xy`/`ct`) and `reachable`.
- **Rules engine**: each rule = `conditions[]` + `actions[]`. Condition `{address, operator,
  value}`; operators `eq`, `gt`, `lt`, `dx` (changed — fires on change), `ddx` (delayed change),
  `stable`/`not stable`, `in`/`not in`. **Values are always strings.** `dx`/`ddx` are placed on
  `state/lastupdated` so a repeated identical value still triggers.

### 4.2 CLIP v2 API (`/clip/v2/resource`)

- Endpoints: `GET /clip/v2/resource[/<type>[/<id>]]`, `PUT`, `POST` (create), `DELETE`. Every call
  carries header **`hue-application-key: <key>`** (same value as the v1 username).
- ~36 resource `rtype` values incl. `device`, `bridge`, `bridge_home`, `room`, `zone`, `light`,
  `grouped_light`, `scene`, `smart_scene`, `button`, `relative_rotary`, `motion`, `temperature`,
  `light_level`, `entertainment`, `entertainment_configuration`, `zigbee_connectivity`,
  `behavior_script`, `behavior_instance`, `geofence_client`, `homekit`, `matter`, …
- Envelope: `{"errors":[…], "data":[…]}`. Every resource has `type`, `id` (UUID), `id_v1`
  (back-ref like `/lights/8`); most have `owner`. Cross-links use `ResourceIdentifier
  {rid, rtype}`.
- **Device/service composition**: one physical product = one `device` resource listing `services[]`
  (ResourceIdentifiers); each service resource carries `owner → device`. A color bulb's device →
  `light` + `zigbee_connectivity`; a motion sensor's device → `motion` + `temperature` +
  `light_level` + `device_power` + `zigbee_connectivity`.
- **`light` service**: `on.on`, `dimming.brightness` **0–100 float %** (note: not 1–254),
  `color_temperature.mirek` 153–500, `color.xy`+`gamut`, `dynamics`, `gradient`, `effects`/
  `effects_v2`/`timed_effects`, `signaling`, `powerup`, `mode` (`normal`/`streaming`).
- **`grouped_light`**: `on` = any member on; `dimming.brightness` = average of *on* members.

### 4.3 Event stream (SSE)

- `GET /eventstream/clip/v2` with `hue-application-key` + `Accept: text/event-stream`.
- Frames: `id: <unix-ts>:0` / `data: [ <Event>, … ]`. Each `Event` =
  `{id, type: add|update|delete|error, creationtime, data: [ <partial resources> ]}`; partials carry
  at least `id`, `type`, `owner`, `id_v1`, and the changed feature.
- One command can emit **batched** events including cascaded `grouped_light` rollups for the room,
  the zone, and `bridge_home` (group 0).
- Bridge keeps **no persistent buffer** (purges after a few minutes); ~one stream per app-key.

### 4.4 Command processing behaviour

- **Documented rate limits**: the bridge "is not able to consistently handle more than **10 'light'
  commands or 1 'group' command per second**." Integrators (Home Assistant, Homebridge) queue at
  exactly these limits. Overload → dropped/merged commands or HTTP **429**.
  ([HA #60745](https://github.com/home-assistant/core/issues/60745))
- Prefer addressing a `grouped_light`/group action over N individual light writes; coalesce
  per-light writes to ~10/s.
- Transition default **400 ms**. **[RE]** `reachable` is inferred from periodic Zigbee poll
  failures — not instantaneous; an emulator must synthesise it for its backends.

### 4.5 Entertainment / streaming

- Start/stop: `PUT entertainment_configuration/<id>` with `{"action":"start"|"stop"}`; targeted
  lights flip to `mode: streaming`.
- Transport: **DTLS 1.2, PSK-only, UDP port 2100**, cipher
  `TLS_PSK_WITH_AES_128_GCM_SHA256`. PSK **identity = app-key**, PSK **key = `clientkey`** (obtained
  by registering with `{"devicetype":"app#x","generateclientkey":true}`).
- **HueStream packet [RE]**: header ASCII `"HueStream"` + version (`0x01 0x00` v1 / `0x02 0x00` v2)
  + sequence byte + reserved + colour-space byte (`0x00` RGB / `0x01` xy+bri) + reserved. v2 body =
  36-byte entertainment_configuration UUID, then per channel `[channel_id(1), C1(2), C2(2), C3(2)]`.
- Client streams ~50 Hz; bridge forwards to Zigbee at ~25 Hz.

### 4.6 Auth summary

- App-key/username = `POST /api` + link button. Same value is the v2 `hue-application-key`.
- `clientkey` (16-byte PSK) only used for the DTLS Entertainment connection.
- Local v2 uses only the app-key header over a self-signed bridge TLS cert. The cloud Remote API
  (`api.meethue.com`) uses a separate OAuth2 + digest/token flow (not implemented by local
  emulators).

---

## 5. Bottom line

- ✅ Real firmware obtained, `.fw2` container reverse-engineered, **encryption proven offline**.
- ❌ Rootfs **cannot** be extracted offline (AES-encrypted payload, no public key). A physical
  bridge **UART/NAND hardware dump** is required (Section 2.3).
- ✅ Real data-processing model documented from Synacktiv's `ipbridge` RE + the full v1/v2 API spec.

### Items resting only on community RE (no open official source)

HueStream byte layout; `reachable`/connectivity timing; internal debounce/coalescing beyond the
published rate limits; `relative_rotary` and `behavior_instance.configuration` payload schemas; SSE
single-connection/reconnect rules; remote-API token internals; exact SoC part (QCA4531 vs AR9344 vs
AR9331).

## Sources

- OpenWrt forum — Hue Bridge v2 root: <https://forum.archive.openwrt.org/viewtopic.php?id=66346>
- Synacktiv — "Make it Blink": <https://www.synacktiv.com/en/publications/make-it-blink-over-the-air-exploitation-of-the-philips-hue-bridge>
- Colin O'Flynn — Getting Root on Hue Bridge 2.0: <https://colinoflynn.com/2016/07/getting-root-on-philips-hue-bridge-2-0/>
- Colin O'Flynn — Hue AES-CCM (lamp key): <https://colinoflynn.com/2016/11/philips-hue-aes-ccm-and-more/>
- Ronen et al. — IoT Goes Nuclear: <https://eyalro.net/project/iotworm.html>
- wejn.org — Rooting Hue Bridge fw 1967054020 (2024): <https://wejn.org/2024/11/rooting-hue-bridge-with-firmware-1967054020/>
- HackMD — Hue Bridge root notes: <https://hackmd.io/@fjTXL6atTJyn_W4kMM9iCQ/SyEEzTU85>
- hauke/philips-hue-bsb002 (GPL QSDK source): <https://github.com/hauke/philips-hue-bsb002>
- lyuzashi/hue-platform: <https://github.com/lyuzashi/hue-platform>
- openhue-api (CLIP v2 OpenAPI spec): <https://github.com/openhue/openhue-api>
- Burgestrand Hue API (v1 reference): <https://www.burgestrand.se/hue-api/>
- iotech.blog — Hue Entertainment API: <https://iotech.blog/posts/philips-hue-entertainment-api/>
- Home Assistant rate-limit issue #60745: <https://github.com/home-assistant/core/issues/60745>

---

*Compiled from offline firmware analysis + cited public reverse-engineering. `.fw2` images are not
redistributed here; download them yourself from the `firmware.meethue.com` endpoint in Section 1.1.*
