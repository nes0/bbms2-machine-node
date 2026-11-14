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

**Current sensor.** The pin sees roughly the peak of the burden resistor's voltage, so the reader works out
`amps ≈ volts × ct_turns ÷ (burden_ohms × 1.414)`. Set `burden_ohms` to the resistor fitted (the default, 100,
is a guess) and `current_zero_mv` to what the pin reads with the machine off. The result is only compared
with the machine's threshold from BBMS, to count motor-on seconds.

## Setting a reader up

1. Flash MicroPython: `ESP32_GENERIC-20251209-v1.27.0.bin`, from micropython.org (not kept in this repository).
2. Copy `config.example.json` to `data/config.json` and fill in the Wi-Fi details and the reader's API key.
3. For `https`, save the certificate authority that signed BBMS's certificate as `data/ca.der` (DER format):
   `openssl x509 -in root.pem -outform DER -out data/ca.der`. Without it the reader refuses to connect,
   because it couldn't tell BBMS from an impostor. (`"tls_insecure": true` skips the check: bench only.)
4. `uv run deploy_app.py` uploads `app/` and those two files. It never deletes the reader's own `/data` files.

The reader's serial console logs every request and reply. While developing, set `"watchdog_seconds": 0`, or
the watchdog restarts the reader a minute after you stop the program.

## Trying it without the hardware

`python app` (needs `textual`) runs the same program in a terminal with pretend hardware: type a card number or
a 12-hex tag and press Tap Fob. It reads `data/config.json` too; for a development server use
`"api_url": "http://localhost:8000/api/devices/v2/"` and the key from `manage.py create_demo_device`.

## How it differs from the fake reader

The logic is the same. The differences come from the e-paper, which takes a few seconds to change:

- The screen is redrawn only when what it shows changes. A session shows whole minutes, so it's redrawn once a
  minute; the charge and balance move on with it.
- A message stays for its time counted from when it has finished appearing.
- There's no "Hello" after a tap: the session screen appears instead, after the first heartbeat.
- A tap or press made while the reader is busy with an earlier one is dropped, not queued.
- If BBMS is away for long, meter readings it can't have seen are added together rather than queued one a
  minute, and one BBMS rejects as malformed (400) is dropped rather than retried for ever.
- The first check-in to succeed is sent as `boot`, even if earlier tries failed; the QR code is asked for again
  after a start-up without BBMS.

Like the fake, it has no offline fob cache: without BBMS nobody can tap in, though a session can always be ended.

## Files

| | |
|---|---|
| `app/main.py` | start-up on the ESP32 |
| `app/reader.py` | the reader's behaviour (no hardware in here) |
| `app/screen.py` | the screens, drawn into the e-paper buffer |
| `app/bbms_api.py` | HTTP(S) requests to BBMS |
| `app/hardware.py` | relay, buttons, RDM6300, current sensor, Wi-Fi, watchdog |
| `app/rdm6300.py` | reading fobs from the RDM6300's serial frames |
| `app/flash.py`, `app/config.py` | what's kept through a power cut; settings |
| `app/epaper213.py`, `app/display_base.py` | e-paper driver |
| `app/__main__.py`, `app/simulation/` | the terminal simulation |
| `app/font/packed_*.py` | the two fonts the reader uses, made by `uv run process_fonts.py` from `font/ari/` |
| `deploy_app.py`, `pyboard.py` | uploading to the reader over USB |
