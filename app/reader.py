# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# The machine reader: a fob tap starts a session and switches the machine on; a tap or the Stop button
# ends it. It talks to BBMS through the v2 device API (docs/device-api-v2.md in the BBMS repository).
#
# This is the fake reader on BBMS's /dev/fake-device/ page (static/access/fake_device.js) rewritten for
# the real hardware, and it should stay in step with it. Where it differs, it's because of the e-paper,
# which takes a few seconds to change and mustn't be changed too often:
#   - the screen is redrawn only when what it shows changes, so a session shows whole minutes;
#   - a message stays up for its time counted from when it has finished appearing;
#   - there's no "Hello" message after a tap: the session screen is the greeting.
#
# Nothing in here touches the hardware directly: that's the `hw` object (hardware.py on the ESP32,
# simulation/sim_hardware.py on a computer).
#

import asyncio
import binascii
import hashlib

from util import log, now_ms, print_exception

FIRMWARE_VERSION = "esp32-0.1"
METER_REPORT_MS = 60000
RETRY_MS = 15000
MESSAGE_SECONDS = 4

REFUSALS = {
    "unknown_fob": ("Fob not recognised",),
    "fob_lost": ("This fob was", "reported lost"),
    "not_member": ("Not a current member",),
    "not_trained": ("Training needed", "for this machine"),
    "course_incomplete": ("Finish the online", "course first"),
    "out_of_order": ("Out of order",),
    "insufficient_credit": ("Not enough credit", "Please top up"),
    "device_disabled": ("Reader disabled",),
    "not_tapped_in": ("Tap in first",),
}


def pounds(pence):
    return "%s£%d.%02d" % ("-" if pence < 0 else "", abs(pence) // 100, abs(pence) % 100)


def duration(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def refusal_text(data):
    reason = data.get("reason")
    if reason == "enrolled":
        return ("Fob registered", "for " + (data.get("display_name") or "a member"))
    if reason == "in_use":
        return ("In use by", data.get("in_use_by") or "another member")
    return REFUSALS.get(reason) or ("Refused", reason or "?")


def summary_text(data):
    if data.get("removed"):
        return ("Session ended", "Under a minute:", "no charge")
    return (
        "Session ended " + duration(data.get("duration_seconds") or 0),
        "Charge " + pounds(data.get("fee_pence") or 0),
        "Balance " + pounds(data.get("balance_pence") or 0),
    )


def first_set(*values):
    """The first value that isn't None (0 counts as set)."""
    for value in values:
        if value is not None:
            return value
    return None


class Session:
    def __init__(self, data, rate, now):
        self.id = data["session_id"]
        self.name = data.get("display_name") or ""
        self.reason = data.get("session_reason") or ""
        self.rate = rate
        # Updated by each heartbeat; the screen counts on from these between heartbeats.
        self.at_heartbeat = now
        self.elapsed = 0
        self.fee = 0
        self.balance = first_set(data.get("balance_pence"), 0)


class Reader:
    def __init__(self, hw, api, flash, screen):
        self.hw = hw
        self.api = api
        self.flash = flash
        self.screen = screen

        # A different API key means a different device: what was kept for the old one is no use.
        key_mark = binascii.hexlify(hashlib.sha256(api.key.encode()).digest()[:4]).decode()
        if flash.get("keyMark") != key_mark:
            flash.update({"keyMark": key_mark, "qrEtag": None, "cacheVersion": None, "meterQueue": []})

        # Everything from here on is lost when the power goes; what must survive is in self.flash.
        self.online = False
        self.disabled = False
        self.machine = None  # the machine's details, from check-in and most replies
        self.threshold = None  # amps above which the motor counts as running
        self.heartbeat_ms = 300000
        self.session_heartbeat_ms = 60000
        self.checked_in = False  # BBMS has been told about this start-up
        self.next_check_in = None  # times are in milliseconds (util.now_ms); None means "not due"
        self.session = None  # the session this reader started (or resumed)
        self.orphan = None  # an open session BBMS knows about but this reader doesn't: (id, name)
        self.pending_end = None  # an end BBMS hasn't confirmed yet: (id, body)
        self.next_session_heartbeat = None
        self.next_end_retry = None
        self.active_ms = 0  # motor-on time since the last meter reading was queued
        self.amps = 0
        self.next_meter_queue = now_ms() + METER_REPORT_MS
        self.next_meter_send = 0
        self.unsent_from = None  # meter readings from this seq on have never been sent
        self.message = None  # [lines, seconds, until]; until is None while it hasn't appeared yet
        self.qr = None  # bytes in BBMS's raw format
        self.qr_settled = False  # BBMS has confirmed the QR code since start-up
        self.qr_version = 0
        self.shown = None  # the view on the screen now

    # ---- Talking to BBMS ----------------------------------------------------------------------

    async def call(self, method, path, body=None, etag=None, binary=False):
        result = await self.api.request(method, path, body, etag, binary)
        self.online = result.ok or result.status == 304
        return result

    # ---- Start-up, check-in and the QR code -----------------------------------------------------

    async def run(self):
        """Never returns."""
        self.update_relay()
        self.show_message(("Starting...",), 0)
        await self.redraw()
        for _ in range(100):  # give the network up to 10 seconds, on top of the time the screen took
            if self.hw.link_up():
                break
            await asyncio.sleep(0.1)
        await self.load_qr()
        await self.check_in()
        self.message = None
        self.hw.discard_inputs()

        asyncio.create_task(self._watch_inputs())
        asyncio.create_task(self._watch_current())
        while True:
            self.hw.feed_watchdog()
            try:
                await self.step()
            except Exception as error:  # carry on: stopping here could leave the machine switched on
                log("error in the main loop:")
                print_exception(error)
            await asyncio.sleep(0.05)

    async def load_qr(self):
        etag = self.flash.get("qrEtag")
        stored = self.flash.read_file("qr.bin")
        result = await self.call("GET", "qr", etag=etag if stored else None, binary=True)
        code = result.data
        if result.status == 200 and code and len(code) == 1 + code[0] * ((code[0] + 7) // 8):
            self.flash.write_file("qr.bin", code)
            self.flash.set("qrEtag", result.etag)
            self.qr = code
            self.qr_version += 1
        elif stored and self.qr is None:
            self.qr = stored  # 304, or BBMS unreachable: use the copy in flash
            self.qr_version += 1
        if result.status in (200, 304):
            self.qr_settled = True  # otherwise it's asked for again once BBMS is back

    async def check_in(self):
        result = await self.call(
            "POST",
            "check-in",
            {
                "event": "heartbeat" if self.checked_in else "boot",
                "firmware_version": FIRMWARE_VERSION,
                # The offline fob cache isn't written yet: this hands back whatever BBMS last said, as
                # the fake reader does, so BBMS doesn't ask for a refresh at every check-in.
                "cache_version": self.flash.get("cacheVersion"),
            },
        )
        if not result.ok:
            self.next_check_in = now_ms() + RETRY_MS
            return
        data = result.data
        self.checked_in = True
        self.heartbeat_ms = (data.get("heartbeat_seconds") or 0) * 1000 or self.heartbeat_ms
        self.session_heartbeat_ms = (data.get("session_heartbeat_seconds") or 0) * 1000 or self.session_heartbeat_ms
        self.next_check_in = now_ms() + self.heartbeat_ms
        if "cache_version" in data and data["cache_version"] != self.flash.get("cacheVersion"):
            self.flash.set("cacheVersion", data["cache_version"])
        commands = data.get("commands") or ()
        self.disabled = "disable" in commands
        for command in commands:
            if command in ("refresh_cache", "purge_cache"):
                log('command "%s": there is no offline fob cache yet' % command)
        if data.get("machine"):
            self.apply_machine(data["machine"])
        if "reboot" in commands:
            log('command "reboot"')
            self.hw.reboot()

    def apply_machine(self, machine):
        self.machine = machine
        session_id = machine.get("session_id")
        if self.session and session_id != self.session.id and not self.pending_end:
            # Ended elsewhere (an admin, the tidy-up or idle auto-end).
            self.session = None
            self.next_session_heartbeat = None
            self.show_message(("Session ended", "by BBMS"))
        if not self.session and session_id and not self.pending_end:
            self.orphan = (session_id, machine.get("in_use_by"))
        elif not session_id:
            self.orphan = None
        try:
            self.threshold = float(machine["meter"]["current_threshold_amps"])
        except (KeyError, TypeError, ValueError):
            self.threshold = None  # metered by tapped-in time
        self.update_relay()

    # ---- Fob taps and buttons -------------------------------------------------------------------

    async def tap(self, card):
        if self.disabled:
            return self.show_message(("Reader disabled", "by an admin"))
        if self.session:
            return await self.end_session({"card": card}, self.session.id)

        result = await self.call("POST", "access", {"card": card})
        if not result.ok:
            return self.show_message(("BBMS unavailable", "Try again shortly"))
        data = result.data
        if data.get("machine"):
            self.apply_machine(data["machine"])
        if data.get("allowed"):
            self.start_local_session(data)
        else:
            self.show_message(refusal_text(data))

    def start_local_session(self, data):
        rate = first_set(data.get("hourly_rate_pence"), (self.machine or {}).get("hourly_rate_pence"), 0)
        self.orphan = None
        self.session = Session(data, rate, now_ms())
        self.next_session_heartbeat = now_ms()  # straight away, to learn the elapsed time and fee
        self.update_relay()

    async def session_heartbeat(self):
        session = self.session
        self.next_session_heartbeat = now_ms() + self.session_heartbeat_ms
        result = await self.call("POST", "sessions/%d/heartbeat" % session.id, {})
        if self.session is not session:
            return
        if result.status == 404:
            self.end_local_session()
            return self.show_message(("Session not found", "Tap in again"))
        if not result.ok:
            return  # keep counting locally; try again next time
        data = result.data
        if data.get("open") is False:
            self.end_local_session()
            return self.show_message(summary_text(data), 8)
        session.at_heartbeat = now_ms()
        session.elapsed = first_set(data.get("elapsed_seconds"), session.elapsed)
        session.fee = first_set(data.get("fee_so_far_pence"), session.fee)
        session.balance = first_set(data.get("balance_pence"), session.balance)

    async def stop_button(self):
        if self.session:
            return await self.end_session({"ended_by": "button"}, self.session.id)
        if self.orphan:  # left open by a power cut
            return await self.end_session({"ended_by": "button"}, self.orphan[0])
        self.show_message(("Nothing to stop",), 2)

    async def end_session(self, body, session_id):
        await self.flush_meter(True)
        result = await self.call("POST", "sessions/%d/end" % session_id, body)
        if not result.ok:
            # Turn the machine off anyway; tell BBMS when it's back.
            self.pending_end = (session_id, body)
            self.next_end_retry = now_ms() + RETRY_MS
            self.end_local_session()
            return self.show_message(("Session ended", "BBMS will be told", "when it's back"))
        data = result.data
        if data.get("ended") is False:
            if data.get("machine"):
                self.apply_machine(data["machine"])
            return self.show_message(refusal_text(data))  # someone else's fob: the session carries on
        self.end_local_session()
        if data.get("machine"):
            self.apply_machine(data["machine"])
        self.show_message(summary_text(data), 8)

    async def retry_pending_end(self):
        session_id, body = self.pending_end
        self.next_end_retry = now_ms() + RETRY_MS
        result = await self.call("POST", "sessions/%d/end" % session_id, body)
        if not result.ok:
            return
        self.pending_end = None
        self.next_end_retry = None
        if result.data.get("machine"):
            self.apply_machine(result.data["machine"])

    def end_local_session(self):
        self.session = None
        self.orphan = None
        self.next_session_heartbeat = None
        self.update_relay()

    async def fault_button(self):
        if not self.session:
            return self.show_message(("Tap in first", "to report a fault"))
        broken = bool(self.machine) and self.machine.get("status") == "out_of_order"
        status = "working" if broken else "out_of_order"
        result = await self.call(
            "POST", "machine-status", {"status": status, "session_id": self.session.id, "note": ""}
        )
        if not result.ok:
            return self.show_message(("BBMS unavailable", "Fault not reported"))
        data = result.data
        if data.get("machine"):
            self.apply_machine(data["machine"])
        if data.get("changed"):
            if status == "working":
                self.show_message(("Marked as fixed", "Thank you"))
            else:
                self.show_message(("Reported out of order", "The team will be told"))
        else:
            self.show_message((data.get("reason") or "No change",))

    def update_relay(self):
        self.hw.set_relay(self.session is not None)

    # ---- Hour meter -----------------------------------------------------------------------------

    def metering_by_current(self):
        return bool(self.machine) and (self.machine.get("meter") or {}).get("mode") == "current"

    async def flush_meter(self, force):
        """Queue a reading (kept in flash) and try to send everything queued."""
        self.next_meter_queue = now_ms() + METER_REPORT_MS
        if not self.metering_by_current():
            return
        seconds = self.active_ms // 1000
        if force or seconds > 0 or self.session:
            self.active_ms -= seconds * 1000
            session_id = self.session.id if self.session else None
            queue = self.flash.get("meterQueue", [])
            last = queue[-1] if queue else None
            if (
                last
                and self.unsent_from is not None
                and last["seq"] >= self.unsent_from
                and last["session_id"] == session_id
            ):
                # BBMS can't have seen the last reading, so add to it: the queue stays short however
                # long BBMS is away. (A reading that might have arrived must be resent unchanged.)
                last["active_seconds"] += seconds
                self.flash.set("meterQueue", queue)
            else:
                seq = self.flash.get("meterSeq", 0) + 1
                queue.append({"seq": seq, "active_seconds": seconds, "session_id": session_id})
                self.flash.update({"meterSeq": seq, "meterQueue": queue})
        await self.send_meter_queue()

    async def send_meter_queue(self):
        while True:
            queue = self.flash.get("meterQueue", [])
            if not queue:
                return
            reading = queue[0]
            self.unsent_from = reading["seq"] + 1
            result = await self.call("POST", "meter", reading)
            if not result.ok and result.status != 400:
                self.next_meter_send = now_ms() + RETRY_MS
                return  # keep it; the same seq is sent again later, and BBMS ignores repeats
            # Accepted, or a 400: BBMS will never take it, and it mustn't hold up the readings behind it.
            self.flash.set("meterQueue", [r for r in queue if r["seq"] != reading["seq"]])

    async def _watch_current(self):
        """Once a second: is the motor running?"""
        last = now_ms()
        while True:
            await asyncio.sleep(1)
            now = now_ms()
            step = min(now - last, 2000)  # if something held the reader up, don't guess what the motor did
            last = now
            try:
                self.amps = self.hw.amps()
                if self.metering_by_current() and self.threshold is not None and self.amps > self.threshold:
                    self.active_ms += step
            except Exception as error:
                print_exception(error)

    # ---- The main loop --------------------------------------------------------------------------

    async def _watch_inputs(self):
        while True:
            try:
                self.hw.poll(now_ms())
            except Exception as error:
                print_exception(error)
            await asyncio.sleep(0.03)

    async def step(self):
        now = now_ms()
        message = self.message
        if message and message[2] is not None and now > message[2]:
            self.message = None

        # One thing at a time: a tap or a press first, otherwise whichever regular job is due.
        hw = self.hw
        card = hw.take_card(now)
        stop = hw.take_stop()
        fault = hw.take_fault()
        if stop or card or fault:
            if stop:
                await self.stop_button()
            elif card:
                await self.tap(card)
            else:
                await self.fault_button()
            hw.discard_inputs()  # whatever was tapped or pressed while that was going on
            now = now_ms()

        if self.next_check_in is not None and now >= self.next_check_in:
            await self.check_in()
        elif self.session and self.next_session_heartbeat is not None and now >= self.next_session_heartbeat:
            await self.session_heartbeat()
        elif self.pending_end and now >= self.next_end_retry:
            await self.retry_pending_end()
        elif now >= self.next_meter_queue:
            await self.flush_meter(False)
        elif self.flash.get("meterQueue") and now >= self.next_meter_send:
            await self.send_meter_queue()
        elif self.online and not self.qr_settled:
            await self.load_qr()

        await self.redraw()

    # ---- Screen ---------------------------------------------------------------------------------

    def show_message(self, lines, seconds=MESSAGE_SECONDS):
        self.message = [tuple(lines), seconds, None]

    def live_session(self):
        """What the session screen shows between heartbeats: counted on locally, in whole minutes."""
        s = self.session
        elapsed = s.elapsed + (now_ms() - s.at_heartbeat) // 1000
        elapsed -= elapsed % 60
        minimum = ((self.machine or {}).get("minimum_minutes") or 15) * 60
        fee = 0 if s.reason else (s.rate * max(elapsed, minimum) + 1800) // 3600
        return elapsed, fee, s.balance + s.fee - fee

    def view(self):
        """The screen as it should be now, as a tuple for screen.render()."""
        offline = not self.online
        machine = self.machine
        if self.message:
            return ("message", self.message[0])
        if self.session:
            s = self.session
            elapsed, fee, balance = self.live_session()
            hours, minutes = elapsed // 3600, elapsed % 3600 // 60
            return (
                "session",
                machine.get("name") or "" if machine else "",
                s.reason.upper(),
                s.name,
                "%d h %02d min" % (hours, minutes) if hours else "%d min" % minutes,
                "No charge" if s.reason else "Charge " + pounds(fee),
                "Balance " + pounds(balance),
                balance < 0,
                offline,
            )
        if self.disabled:
            return ("notice", "Reader disabled", "Ask an admin", self.qr_version, False)
        if not machine:
            if offline:
                if not self.hw.link_up():
                    return ("notice", "No Wi-Fi", "Retrying...", self.qr_version, False)
                return ("notice", "BBMS unavailable", "Retrying...", self.qr_version, False)
            return ("notice", "Not set up", "No machine for this reader", self.qr_version, False)
        name = machine.get("name") or ""
        if machine.get("status") == "out_of_order":
            return ("broken", name, machine.get("status_note") or "", self.qr_version, offline)
        if self.orphan:
            return ("orphan", name, self.orphan[1] or "", self.qr_version, offline)
        rate = machine.get("hourly_rate_pence")
        return (
            "ready",
            name,
            pounds(rate) + " / hour" if rate else "Free to use",
            "Minimum %s min" % machine.get("minimum_minutes") if rate else "",
            self.qr_version,
            offline,
        )

    async def redraw(self):
        """Bring the screen up to date, if what it should show has changed."""
        view = self.view()
        message = self.message
        if view != self.shown:
            self.screen.render(view, self.qr)
            if not await self.hw.epd.display_async():
                log("the display didn't finish updating")
            self.shown = view
        if message and message[2] is None:
            message[2] = now_ms() + message[1] * 1000  # its time starts once it can be read
