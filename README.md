# Machine reader (ESP32, MicroPython)

A fob reader for one machine: a fob tap starts a session in BBMS and switches the machine on; a tap or the
Stop button ends it. It is the fake reader on BBMS's `/dev/fake-device/` page (`static/access/fake_device.js`)
rewritten for the real hardware, and talks to BBMS through the v2 device API (`docs/device-api-v2.md`).

## Hardware

ESP32 (MicroPython v1.27.0, `ESP32_GENERIC`), a 2.13" 250 × 122 e-paper display, an RDM6300 fob reader, a
relay, two push buttons and a current transformer on the machine's supply.

| What | GPIO | Notes |
|---|---|---|
| E-paper SCK, MOSI, CS, DC, RST, BUSY | 18, 23, 5, 17, 16, 4 | set in `app/epaper213.py` |
| RDM6300 TX → ESP32 RX | 27 | 9600 baud. The RDM6300 is a 5 V part: use a divider or level shifter |
| (UART TX, not connected) | 25 | the UART has to be given a spare pin, or it takes 17 from the display |
| Relay | 26 | high = on; set `"relay_active_high": false` for an active-low relay board |
| Stop button | 32 | button to ground; internal pull-up |
| Fault button | 33 | button to ground; internal pull-up |
| Current sensor | 34 | must be an ADC1 pin (32-39): ADC2 doesn't work while Wi-Fi is on |

Every pin except the display's can be changed in `data/config.json` (the names are in `app/config.py`).

**Current sensor.** A current transformer on the machine's supply feeds an op-amp rectifier and filter, which
gives the pin a steady voltage: about 0.29 V with the machine off, rising by about 62 mV for each amp. The
circuit, its part values and the results of simulating it are in `current_sensor/README.md`. The reader measures the zero itself whenever the relay has been off for five
seconds, so there is nothing to calibrate. For a different circuit, set `current_mv_per_amp` and
`current_zero_mv` (what the pin reads with the machine off). The result is only compared with the machine's
threshold from BBMS, to count motor-on seconds.

## Setting a reader up

1. Flash MicroPython: `ESP32_GENERIC-20251209-v1.27.0.bin`, from micropython.org (not kept in this repository).
2. Copy `config.example.json` to `data/config.json` and fill in the Wi-Fi details and the reader's API key.
3. For `https`, save the certificate authority that signed BBMS's certificate as `data/ca.der` (DER format):
   `openssl x509 -in root.pem -outform DER -out data/ca.der`. Without it the reader refuses to connect,
   because it couldn't tell BBMS from an impostor. (`"tls_insecure": true` skips the check: bench only.)
4. `uv run deploy_app.py` compiles `app/` and uploads it with those two files. It never deletes the reader's
   own `/data` files.

The reader is sent compiled `.mpy` files, not the `.py` sources: the ESP32 hasn't the memory to compile a file
the size of `reader.py` itself. `build.py` does the compiling, into `build/app`, with `mpy-cross`, which `uv run`
installs. Only `main.py` goes as source, because MicroPython won't run a compiled one at start-up. `mpy-cross`
must match the MicroPython on the reader: if you flash a newer MicroPython, change the `mpy-cross` version at
the top of `deploy_app.py` and `make_release.py` to match.

**Memory is tight.** The ESP32 has about 240 KB, shared between MicroPython and the system (Wi-Fi, and the
secure connection to BBMS, which needs about 40 KB). This firmware needs a little more than MicroPython starts
with, and left alone MicroPython would then take so much that no secure connection could be made. `app/start.py`
prevents that by holding the connections' memory back while the program loads; the comment there explains it, and
at start-up it logs four lines beginning `memory` that show how the memory was shared out. `system ... free` in the
last of them should be above 40000. A module with extra RAM (ESP32-WROVER, or an S3 with PSRAM) and MicroPython's
SPIRAM build would remove the limit, but those modules use GPIO 16 and 17, which the e-paper is wired to here.

The reader's serial console logs every request and reply. While developing, set `"watchdog_seconds": 0`, or
the watchdog restarts the reader a minute after you stop the program.

## Updating readers over the network

Once a reader has been set up over USB, BBMS can send it new firmware:

1. Change `VERSION` in `app/version.py`. Readers and BBMS tell firmware apart by this name alone, and BBMS
   refuses a version it has already been given.
2. `uv run make_release.py` compiles the firmware and makes `dist/reader-<version>.zip`.
3. In BBMS's admin, **Access > Firmware releases > Add**: upload the zip, then tick the readers to run it.
   Try one reader first.

Each chosen reader finds out at its next check-in (every five minutes). When nobody is using the machine it
shows "Updating software", downloads the files that have changed, checks each against its SHA-256, and
restarts. BBMS's device list shows the version each reader runs, and "up to date", "waiting to update" or
"update failed" with the reason.

A reader keeps its old firmware until the new one has checked in with BBMS. If the new firmware fails to
start, or starts three times without reaching BBMS (ten minutes each time), the reader puts the old firmware
back and tells BBMS why. It won't try that version again until an admin uses **Try a failed firmware update
again** on the device list, or chooses a different release.

What an update can't change:

- `app/main.py`, the loader that puts updates in place and takes them off again. It is deliberately small and
  uses nothing else from the firmware. Changing it needs `deploy_app.py` and a USB cable.
- The reader's settings and certificate in `/data`.
- MicroPython itself.

Deploying over USB wipes the reader's record of updates (`/ota`), so afterwards it runs exactly what was
deployed. Anyone who can choose readers' firmware in BBMS can run code on them: the reader trusts BBMS, over
the connection its certificate check protects, and releases are not signed separately.

`python -m unittest discover tests` tests the update steps on a computer, including a power cut at each point
while files are being moved.

## When BBMS can't be reached

The reader carries on as best it can and tells BBMS afterwards.

**What counts as "can't be reached".** A request that got no answer from BBMS itself:

| What happened | The screen says |
|---|---|
| Wi-Fi is down | No Wi-Fi |
| BBMS's name can't be looked up (DNS) | Server not found |
| Nothing answers at its address, or the route is broken | Can't connect, or No answer |
| The certificate has run out, or isn't the one expected | Certificate problem, or Secure link failed |
| A proxy in front of BBMS answers 500 or above | Server error 502 |
| Something else answers in BBMS's place (a Wi-Fi log-in page) | Unexpected answer |

A "no" from BBMS is different. If BBMS answers 401 because the key is wrong, or 409 because the machine has been
removed, the reader does not use its own judgement: nobody can tap in.

If only the name look-up fails, the reader isn't cut off at all: it uses the address BBMS last answered from. Over
https the certificate is still checked against the name, so nothing else can answer in BBMS's place.

**What the reader does then.**

- **Taps are judged from the fob list.** BBMS gives each reader the list of members who may use its machine
  (`allowed-fobs` in the device API). The reader keeps it in flash and fetches it again when a check-in says it
  has changed, so it is at most five minutes old when BBMS goes away. A member on the list gets an *offline
  session*: the machine switches on, the screen says OFFLINE, and the reader times the session itself.
- **Credit can't be checked.** BBMS works out the charge when it hears of the session, so a member with no
  credit can run up a debt while BBMS is away. Set `"offline_use": false` in `data/config.json` to stop
  offline sessions; then nobody can tap in without BBMS.
- **A machine last known to be out of order stays off**, and a reader an admin has disabled stays disabled.
- **Only the member who tapped in can tap out** of an offline session. The Stop button ends it for anyone.
- **Sessions BBMS started carry on.** If one is ended while BBMS is away, the machine switches off and BBMS is
  told when it's back, with the time it really ended.
- **BBMS closes a session it hasn't heard from for an hour.** If the member is still at the machine when BBMS
  comes back, they aren't cut off: the reader carries on with an offline session from where BBMS's ended.
- **Nothing is lost in a power cut.** Finished offline sessions, an end waiting to be sent, and meter readings
  are in flash. An offline session that was running when the power went is recorded as ending at that moment,
  to the nearest minute.
- **The reader starts up without BBMS** using the machine's details from last time.

While BBMS is away the reader only tries check-in: every 15 seconds for the first minute, then once a minute.
Everything waiting is sent after the first check-in that gets through. A tap in that time is answered from the
list at once, without waiting for a reply that won't come.

**Limits.**

- The reader holds at most 50 finished offline sessions. After that nobody else can tap in offline until BBMS
  has taken them.
- The ESP32 has no clock of its own: it takes the time from BBMS at check-in. A reader that starts up without
  BBMS counts on from the last time it saved, so its times are early by however long it was switched off. If
  BBMS comes back before the next power cut, the times are put right before they're sent. If not, they're sent
  marked `"clock": "estimated"`. The length of each session is right either way.
- A session BBMS started, ended here during an outage of more than an hour, is charged only up to the last
  heartbeat BBMS answered, because BBMS has closed it by then.
- The Fault button needs BBMS.
- A fob the reader has never been told about can't be used offline: not a new member's since the last
  check-in, and none at all on a reader that has never reached BBMS.

## Trying it without the hardware

`python app` (needs `textual`) runs the same program in a terminal with pretend hardware: type a card number or
a 12-hex tag and press Tap Fob. The motor switch and the amps box beside it set what the machine draws. It reads `data/config.json` too; for a development server use
`"api_url": "http://localhost:8000/api/devices/v2/"` and the key from `manage.py create_demo_device`.
The Network switch is the Wi-Fi: turn it off to see the reader on its own. Stopping the development server
shows a different failure.

## How it differs from the fake reader

The logic is the same. The differences come from the e-paper, which takes a few seconds to change:

- The screen is redrawn only when what it shows changes. A session shows whole minutes, so it's redrawn once a
  minute; the charge and balance move on with it.
- A message stays for its time counted from when it has finished appearing.
- There's no "Hello" after a tap: the session screen appears instead, after the first heartbeat.
- A tap or press made while the reader is busy with an earlier one is dropped, not queued.
- While reporting live current, the reading on the screen changes at most every 5 seconds.
- If BBMS is away for long, meter readings it can't have seen are added together rather than queued one a
  minute, and one BBMS rejects as malformed (400) is dropped rather than retried for ever.
- The first check-in to succeed is sent as `boot`, even if earlier tries failed; the QR code is asked for again
  after a start-up without BBMS.

Two things it does that the fake doesn't: it updates its own firmware, and it lets members on its fob list use
the machine while BBMS can't be reached ([When BBMS can't be reached](#when-bbms-cant-be-reached)). With the fake,
nobody can tap in without BBMS.

## Files

| | |
|---|---|
| `app/main.py` | the loader: what the ESP32 runs first. Puts firmware updates in place, starts the reader, and undoes an update that doesn't work |
| `app/start.py` | starting the reader |
| `app/version.py` | this firmware's version |
| `app/ota.py` | downloading a firmware update from BBMS |
| `app/reader.py` | the reader's behaviour (no hardware in here) |
| `app/fobs.py` | the list of fobs to accept while BBMS can't be reached |
| `app/offline.py` | the time, and the sessions waiting to be sent to BBMS |
| `app/screen.py` | the screens, drawn into the e-paper buffer |
| `app/bbms_api.py` | HTTP(S) requests to BBMS |
| `app/hardware.py` | relay, buttons, RDM6300, current sensor, Wi-Fi, watchdog |
| `app/rdm6300.py` | reading fobs from the RDM6300's serial frames |
| `app/flash.py`, `app/config.py` | what's kept through a power cut; settings |
| `app/epaper213.py`, `app/display_base.py` | e-paper driver |
| `app/__main__.py`, `app/simulation/` | the terminal simulation |
| `app/font/packed_*.py` | the two fonts the reader uses, made by `uv run process_fonts.py` from `font/ari/` |
| `build.py` | compiling the firmware for the reader; used by the two scripts below |
| `deploy_app.py`, `pyboard.py` | uploading to the reader over USB |
| `make_release.py` | building a release to upload to BBMS |
| `tests/` | tests that run on a computer |
