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
# And it does two things the fake doesn't: it updates its own firmware (main.py), and it carries on when
# BBMS can't be reached, as described next.
#
# When BBMS can't be reached. "Can't be reached" means a request got no answer from BBMS itself: no Wi-Fi,
# no DNS, no route, a certificate that has run out, a proxy's 502 (bbms_api.py lists them). Then:
#   - Only check-in is tried, every 15 seconds at first and then every minute. Everything else waits for
#     one to get through, so that the reader isn't forever waiting for a reply when someone taps.
#   - A tap is judged from the fob list BBMS gave earlier (fobs.py). A member on it gets an "offline
#     session", which is run and timed here and sent to BBMS afterwards (offline.py). Credit can't be
#     checked. A machine last known to be out of order stays off.
#   - A session BBMS started carries on. If it is ended here, BBMS is told when it's back, with the time.
#     If BBMS meanwhile gave up on it for want of heartbeats, it carries on here as an offline session.
#   - Meter readings queue up, as they always did.
# An answer of "no" from BBMS (a 401 because the key is wrong, say) is not "can't be reached": the reader
# doesn't use its own judgement against BBMS's.
#
# Nothing in here touches the hardware directly: that's the `hw` object (hardware.py on the ESP32,
# simulation/sim_hardware.py on a computer).
#

import asyncio
import binascii
import hashlib

from fobs import MAX_BYTES, FobList
from offline import Offline
from util import log, now_ms, print_exception
from version import VERSION

METER_REPORT_MS = 60000
CURRENT_REPORT_MS = 2000  # live current, while an admin chooses the threshold (BBMS spec 05 §5.4)
CURRENT_DISPLAY_MS = 5000  # the e-paper takes a few seconds to change: show a new reading at most this often
MAX_CURRENT_SAMPLES = 60
RETRY_MS = 15000
QUICK_RETRIES = 4  # after this many check-ins have failed one after another, try only every PROBE_MS
PROBE_MS = 60000
FOBS_SECONDS = 30  # how long downloading the fob list may take
# An offline session that takes over from one BBMS gave up on starts this long after the last heartbeat
# BBMS answered, so that the two can't overlap however the reader's clock and BBMS's differ.
TAKE_OVER_SECONDS = 10
# A firmware update on trial that hasn't reached BBMS in this long gets a restart; after a few of those
# the loader (main.py) puts the old firmware back.
TRIAL_MS = 600000
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
        self.card = None  # the fob that started it, if it was tapped here
        self.offline = False  # run by this reader alone; BBMS hears of it afterwards (then id is None)
        self.started = None  # for an offline session: when it began (a stamp, see offline.py)
        self.heard = None  # when BBMS last answered a heartbeat (a stamp)
        self.unheard = False  # a heartbeat has been missed since
        self.motor_ms = 0  # motor-on time, for an offline session's record


class Reader:
    def __init__(self, hw, api, flash, screen, updater=None, offline_use=True):
        self.hw = hw
        self.offline_use = offline_use  # may members on the fob list use the machine while BBMS is away?
        self.updater = updater  # firmware updates (updater.py); None where there are none, as in the simulation
        self.update_wanted = None  # the firmware version BBMS wants here, while it differs from this one
        self.update_error = None  # (version, why the last attempt to download it failed)
        self.api = api
        self.flash = flash
        self.screen = screen

        # A different API key means a different device: what was kept for the old one is no use.
        key_mark = binascii.hexlify(hashlib.sha256(api.key.encode()).digest()[:4]).decode()
        self.fobs = FobList(flash.directory + "/fobs.txt")
        if flash.get("keyMark") != key_mark:
            if flash.get("offlineEvents") or flash.get("pendingEnd"):
                log("the API key has changed: dropping what was waiting to be sent for the old one")
            self.fobs.clear()
            flash.update(
                {
                    "keyMark": key_mark,
                    "qrEtag": None,
                    "cacheVersion": None,
                    "meterQueue": [],
                    "machine": None,
                    "disabled": False,
                    "offlineEvents": [],
                    "offlineSession": None,
                    "pendingEnd": None,
                }
            )
        self.away = Offline(flash)  # the time, and sessions waiting to be sent
        address = flash.get("address")  # [BBMS's name, the address it last answered from]
        if address and address[0] == api.host:
            api.address = address[1]

        # Everything from here on is lost when the power goes; what must survive is in self.flash.
        self.online = False
        self.trouble = None  # why the last request failed, in a few words
        self.trouble_detail = None  # and more about it, for whoever is finding out why
        self.failures = 0  # check-ins that have failed one after another
        self.disabled = bool(flash.get("disabled"))
        self.machine = flash.get("machine")  # the machine's details, from check-in and most replies
        self.threshold = None  # amps above which the motor counts as running
        self.set_threshold()
        self.fobs_wanted = False  # BBMS has a fob list this reader doesn't hold
        self.next_event_send = 0
        self.heartbeat_ms = 300000
        self.session_heartbeat_ms = 60000
        self.checked_in = False  # BBMS has been told about this start-up
        self.next_check_in = None  # times are in milliseconds (util.now_ms); None means "not due"
        self.session = None  # the session this reader started (or resumed)
        self.orphan = None  # an open session BBMS knows about but this reader doesn't: (id, name)
        # An end BBMS hasn't confirmed yet: {"id", "body", "at": a stamp}. Kept in flash too.
        self.pending_end = flash.get("pendingEnd")
        self.next_session_heartbeat = None
        self.next_end_retry = 0
        self.active_ms = 0  # motor-on time since the last meter reading was queued
        self.amps = 0
        self.current_samples = []  # one a second while BBMS asks for live current
        self.next_current_send = 0
        self.shown_amps = 0  # the reading on the screen, changed at most every CURRENT_DISPLAY_MS
        self.next_amps_shown = 0
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
        was_online = self.online
        self.online = not result.cut_off
        self.trouble = result.why
        self.trouble_detail = result.detail
        if was_online and not self.online:
            log("BBMS can't be reached:", result.why)
            soon = now_ms() + RETRY_MS  # from here on only check-in is tried
            if self.next_check_in is None or self.next_check_in > soon:
                self.next_check_in = soon
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
                "firmware_version": VERSION,
                "cache_version": self.flash.get("cacheVersion"),  # of the fob list held (fobs.py)
                "firmware_update": self.update_report(),
            },
        )
        if not result.ok:
            self.failures += 1
            self.next_check_in = now_ms() + (RETRY_MS if self.failures <= QUICK_RETRIES else PROBE_MS)
            return
        if self.failures:
            log("BBMS is back")
        self.failures = 0
        data = result.data
        if data.get("server_time"):
            self.away.set_clock(data["server_time"])
        if self.api.address and self.flash.get("address") != [self.api.host, self.api.address]:
            self.flash.set("address", [self.api.host, self.api.address])
        if self.updater and self.updater.on_trial:
            self.updater.confirm()  # this firmware works well enough to reach BBMS: keep it
        self.checked_in = True
        self.heartbeat_ms = (data.get("heartbeat_seconds") or 0) * 1000 or self.heartbeat_ms
        self.session_heartbeat_ms = (data.get("session_heartbeat_seconds") or 0) * 1000 or self.session_heartbeat_ms
        self.next_check_in = now_ms() + self.heartbeat_ms
        commands = data.get("commands") or ()
        self.disabled = "disable" in commands
        if self.disabled != bool(self.flash.get("disabled")):
            self.flash.set("disabled", self.disabled)  # so it holds if the reader starts up without BBMS
        if "purge_cache" in commands:
            log('command "purge_cache"')
            self.fobs.clear()
            self.flash.set("cacheVersion", None)
        version = data.get("cache_version")  # "" when BBMS has no list for this reader
        self.fobs_wanted = bool(version) and (version != self.flash.get("cacheVersion") or "refresh_cache" in commands)
        if data.get("machine"):
            self.apply_machine(data["machine"])
        self.note_firmware(data, commands)
        if "reboot" in commands:
            log('command "reboot"')
            self.hw.reboot()

    # ---- Firmware updates (the sequence is described in main.py) ---------------------------------

    def update_report(self):
        """What to tell BBMS about an update that hasn't worked: {"version", "error"}, or None."""
        failure = self.updater.failure() if self.updater else None
        if failure:
            return {"version": failure.get("version"), "error": "rolled back: %s" % failure.get("error")}
        if self.update_error:
            return {"version": self.update_error[0], "error": self.update_error[1]}
        return None

    def note_firmware(self, data, commands):
        wanted = (data.get("firmware") or {}).get("version")
        self.update_wanted = None
        if not self.updater:
            if wanted and wanted != VERSION:
                log("BBMS wants firmware", wanted, "here: there are no updates in the simulation")
            return
        if "retry_firmware" in commands:
            self.updater.forget_failure()
        failure = self.updater.failure()
        if wanted and wanted != VERSION and not (failure and failure.get("version") == wanted):
            self.update_wanted = wanted
        if self.update_error and self.update_error[0] != wanted:
            self.update_error = None

    async def update_firmware(self):
        """Download the firmware BBMS wants and restart into it. Only when nobody is using the machine."""
        version, self.update_wanted = self.update_wanted, None  # one try; the next check-in asks again
        log("updating firmware from", VERSION, "to", version)
        self.show_message(("Updating software", "Please wait"), 0)
        await self.redraw()
        try:
            await self.updater.fetch(self.api, version, self.hw.feed_watchdog)
        except Exception as error:  # UpdateError, a full flash, anything: carry on with this firmware
            log("firmware update failed:", error)
            self.update_error = (version, str(error)[:200])
            self.message = None
            self.hw.discard_inputs()
            return
        self.hw.reboot()

    # ---- The fob list for when BBMS is away (fobs.py) ---------------------------------------------

    async def load_fobs(self):
        self.fobs_wanted = False  # one try; the next check-in asks again
        reply = self.fobs.path + ".json"
        self.hw.feed_watchdog()
        result = await self.api.download("allowed-fobs", reply, FOBS_SECONDS, MAX_BYTES)
        self.hw.feed_watchdog()
        try:
            if result.status != 200:
                raise ValueError(result.status or result.error)
            version, count = self.fobs.store(reply)
        except Exception as error:  # the old list, if there is one, is still there
            log("couldn't fetch the fob list:", error)
            return
        self.flash.set("cacheVersion", version)
        log("fob list", version, "stored:", count, "fobs")

    # ---- The machine's details ------------------------------------------------------------------

    def set_threshold(self):
        try:
            self.threshold = float(self.machine["meter"]["current_threshold_amps"])
        except (KeyError, TypeError, ValueError):
            self.threshold = None  # metered by tapped-in time

    def apply_machine(self, machine):
        self.machine = machine
        # Kept in flash, less what changes from minute to minute, for a start-up without BBMS.
        kept = dict(machine)
        kept["session_id"] = kept["in_use_by"] = None
        kept["meter"] = dict(machine.get("meter") or {})
        kept["meter"]["report_current"] = False
        if kept != self.flash.get("machine"):
            self.flash.set("machine", kept)

        session_id = machine.get("session_id")
        session = self.session
        if session and not session.offline and session_id != session.id and not self.pending_end:
            # Ended elsewhere (an admin, the tidy-up or idle auto-end).
            self.session_ended_by_bbms(None)
        if not self.session and session_id and not self.pending_end:
            self.orphan = (session_id, machine.get("in_use_by"))
        elif not session_id:
            self.orphan = None
        self.set_threshold()
        self.update_relay()

    def session_ended_by_bbms(self, summary):
        session = self.session
        if session.unheard and session.card and self.offline_use:
            # BBMS gave up on it because it couldn't hear from here. The member is still at the machine,
            # so carry on as an offline session from where BBMS left off: BBMS ended its own at the
            # last heartbeat it answered.
            log("BBMS closed session", session.id, "while it was away: carrying on offline")
            session.started = dict(session.heard)
            session.started["up"] += TAKE_OVER_SECONDS
            if session.started["at"] is not None:
                session.started["at"] += TAKE_OVER_SECONDS
            session.id = None
            session.offline = True
            session.reason = ""
            self.next_session_heartbeat = None
            self.keep_offline_session()
            return
        self.end_local_session()
        self.show_message(summary_text(summary) if summary else ("Session ended", "by BBMS"), 8 if summary else 4)

    # ---- Fob taps and buttons -------------------------------------------------------------------

    async def tap(self, card):
        log("fob read:", card)  # so that a tap nobody made can be told from a real fob
        session = self.session
        if session and session.offline:
            if card == session.card:
                return self.end_offline_session("tap")
            return self.show_message(("In use by", session.name or "another member", "Press Stop to end"))
        if self.disabled:
            return self.show_message(("Reader disabled", "by an admin"))
        if session:
            return await self.end_session({"card": card}, session.id)

        # While BBMS is known to be away there's no asking it: that could keep the member waiting for
        # the whole of the time allowed for a reply.
        result = await self.call("POST", "access", {"card": card}) if self.online else None
        if result is None or result.cut_off:
            return self.offline_tap(card)
        if not result.ok:
            return self.show_message(("BBMS turned the", "reader away (%d)" % result.status))
        data = result.data
        if data.get("machine"):
            self.apply_machine(data["machine"])
        if data.get("allowed"):
            self.start_local_session(data, card)
        else:
            self.show_message(refusal_text(data))

    def start_local_session(self, data, card):
        rate = first_set(data.get("hourly_rate_pence"), (self.machine or {}).get("hourly_rate_pence"), 0)
        self.orphan = None
        self.session = Session(data, rate, now_ms())
        self.session.card = card
        self.session.heard = self.away.stamp()
        self.next_session_heartbeat = now_ms()  # straight away, to learn the elapsed time and fee
        self.update_relay()

    # ---- Without BBMS: the reader's own judgement ---------------------------------------------------

    def offline_tap(self, card):
        """A tap that BBMS can't be asked about."""
        why = self.trouble or "No answer"
        machine = self.machine
        if not self.offline_use or not machine or not self.flash.get("cacheVersion"):
            return self.show_message(("Can't reach BBMS", why, "Try again shortly"))
        if machine.get("status") == "out_of_order":
            return self.show_message(("Out of order",))
        name = self.fobs.find(card)
        if name is None:
            # Not trained, not a member, or added since the list was fetched: there's no telling which.
            return self.show_message(("Can't reach BBMS", why, "Fob not on the offline list"))
        if self.away.full():
            return self.show_message(("Can't reach BBMS", why, "Offline record is full"))
        log("BBMS can't be reached (%s): starting an offline session for a fob on the list" % why)
        self.session = Session(
            {"session_id": None, "display_name": name}, machine.get("hourly_rate_pence") or 0, now_ms()
        )
        self.session.card = card
        self.session.offline = True
        self.session.started = self.away.stamp()
        self.keep_offline_session()
        self.update_relay()

    def keep_offline_session(self):
        """Write the offline session down, so a power cut loses a minute of it at most."""
        session = self.session
        self.away.keep_session(session.card, session.name, session.started, session.motor_ms // 1000)

    def end_offline_session(self, ended_by):
        session = self.session
        elapsed = (now_ms() - session.at_heartbeat) // 1000
        self.away.add_session(session.card, session.started, self.away.stamp(), ended_by, session.motor_ms // 1000)
        self.end_local_session()
        self.next_event_send = 0
        self.show_message(("Session ended " + duration(elapsed), "BBMS will be told", "when it's back"), 8)

    async def send_events(self):
        """Send the offline sessions that have finished. BBMS answers for each; whatever it says, that one is done."""
        events = self.away.batch()
        result = await self.call("POST", "offline-events", {"events": events})
        if not result.ok and result.status != 400:
            self.next_event_send = now_ms() + RETRY_MS
            return  # keep them; BBMS takes each id only once, so sending again is safe
        results = {}
        if result.ok:
            for item in result.data.get("results") or ():
                results[item.get("id")] = item.get("result")
        for event in events:
            # No result for one, or a 400 for the lot: BBMS will never take it, and it mustn't hold up the rest.
            outcome = results.get(event["id"], "refused")
            if outcome not in ("recorded", "duplicate"):
                log("BBMS didn't record offline session", event["id"], "-", outcome)
        self.away.sent([event["id"] for event in events])

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
            session.unheard = True
            return  # keep counting locally; try again next time
        data = result.data
        if data.get("open") is False:
            return self.session_ended_by_bbms(data)
        session.heard = self.away.stamp()
        session.unheard = False
        session.at_heartbeat = now_ms()
        session.elapsed = first_set(data.get("elapsed_seconds"), session.elapsed)
        session.fee = first_set(data.get("fee_so_far_pence"), session.fee)
        session.balance = first_set(data.get("balance_pence"), session.balance)

    async def stop_button(self):
        if self.session and self.session.offline:
            return self.end_offline_session("button")
        if self.session:
            return await self.end_session({"ended_by": "button"}, self.session.id)
        if self.orphan:  # left open by a power cut
            return await self.end_session({"ended_by": "button"}, self.orphan[0])
        self.show_message(("Nothing to stop",), 2)

    async def end_session(self, body, session_id):
        await self.flush_meter(True)
        result = await self.call("POST", "sessions/%d/end" % session_id, body) if self.online else None
        if result is None or result.cut_off:
            # Turn the machine off anyway; tell BBMS when it's back, and when this was.
            self.pending_end = {"id": session_id, "body": body, "at": self.away.stamp()}
            self.flash.set("pendingEnd", self.pending_end)
            self.next_end_retry = now_ms() + RETRY_MS
            self.end_local_session()
            return self.show_message(("Session ended", "BBMS will be told", "when it's back"))
        if not result.ok:  # a 404: BBMS has no such session on this machine
            self.end_local_session()
            return self.show_message(("Session ended", "BBMS has no", "record of it"))
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
        pending = self.pending_end
        self.next_end_retry = now_ms() + RETRY_MS
        body = dict(pending["body"])
        body["ended_at"] = self.away.when(pending["at"])[0]  # BBMS ends it then, not when this arrives
        result = await self.call("POST", "sessions/%d/end" % pending["id"], body)
        if result.cut_off:
            return
        if not result.ok:
            log("BBMS won't take the end of session", pending["id"], "-", result.status)
        self.pending_end = None
        self.flash.set("pendingEnd", None)
        if result.ok and result.data.get("machine"):
            self.apply_machine(result.data["machine"])

    def end_local_session(self):
        self.session = None
        self.orphan = None
        self.next_session_heartbeat = None
        self.update_relay()

    async def fault_button(self):
        if not self.session:
            return self.show_message(("Tap in first", "to report a fault"))
        if self.session.offline or not self.online:
            return self.show_message(("Can't reach BBMS", "Fault not reported"))
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
        if self.online:
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

    # ---- Live current: while an admin is choosing the threshold, send every reading --------------

    def reporting_current(self):
        return bool(self.machine) and bool((self.machine.get("meter") or {}).get("report_current"))

    async def send_current(self):
        self.next_current_send = now_ms() + CURRENT_REPORT_MS
        amps, self.current_samples = self.current_samples, []
        result = await self.call("POST", "current", {"amps": amps})
        # Not 200: those readings are lost, which doesn't matter; they're only for watching.
        if result.ok and result.data.get("report_current") is False and self.machine:
            self.machine.setdefault("meter", {})["report_current"] = False  # BBMS has stopped watching

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
                    if self.session:
                        self.session.motor_ms += step
                if self.reporting_current():
                    self.current_samples.append(round(self.amps, 2))
                    if len(self.current_samples) > MAX_CURRENT_SAMPLES:  # BBMS away: keep only the latest
                        self.current_samples = self.current_samples[-MAX_CURRENT_SAMPLES:]
                elif self.current_samples:
                    self.current_samples = []
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
            await self.redraw()  # the answer first: nothing else is taken in until it can be read
            # Whatever was tapped or pressed while that was going on is dropped. That includes the same
            # fob, still held on the reader: it has to be taken away before it counts as another tap.
            hw.discard_inputs()
            now = now_ms()

        idle = not (self.session or self.pending_end or self.message)
        if self.update_wanted and idle:
            await self.update_firmware()
        elif self.updater and self.updater.on_trial and idle and now > TRIAL_MS:
            log("the new firmware hasn't reached BBMS: restarting")
            self.hw.reboot()
        elif self.next_check_in is not None and now >= self.next_check_in:
            await self.check_in()
        elif now >= self.next_meter_queue:
            if self.session and self.session.offline:
                self.keep_offline_session()
            await self.flush_meter(False)
        elif not self.online:
            # Everything below waits for a check-in to get through.
            if self.session and self.next_session_heartbeat is not None and now >= self.next_session_heartbeat:
                self.session.unheard = True
        elif self.session and self.next_session_heartbeat is not None and now >= self.next_session_heartbeat:
            await self.session_heartbeat()
        elif self.pending_end and now >= self.next_end_retry:
            await self.retry_pending_end()
        elif self.flash.get("meterQueue") and now >= self.next_meter_send:
            await self.send_meter_queue()
        elif self.flash.get("offlineEvents") and now >= self.next_event_send:
            await self.send_events()
        elif self.fobs_wanted and idle:
            await self.load_fobs()
        elif self.reporting_current() and self.current_samples and now >= self.next_current_send:
            await self.send_current()
        elif not self.qr_settled:
            await self.load_qr()

        if now >= self.next_amps_shown:
            self.shown_amps = self.amps
            self.next_amps_shown = now + CURRENT_DISPLAY_MS

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
        if self.reporting_current():
            amps = self.shown_amps
            threshold = (machine.get("meter") or {}).get("current_threshold_amps")
            try:
                state = "Threshold %s A: %s" % (threshold, "running" if amps > float(threshold) else "idle")
            except (TypeError, ValueError):
                state = "No threshold set"
            return (
                "current",
                machine.get("name") or "",
                "%.1f A" % amps,
                state,
                "Tap fob or press Stop to end" if self.session else "Tap in to switch it on",
            )
        if self.session:
            s = self.session
            elapsed, fee, balance = self.live_session()
            hours, minutes = elapsed // 3600, elapsed % 3600 // 60
            time = "%d h %02d min" % (hours, minutes) if hours else "%d min" % minutes
            if s.offline:
                # BBMS works out the charge when it hears of the session: what's shown is a guide.
                charge = "About " + pounds(fee) if s.rate else "Free to use"
                return ("session", "OFFLINE", s.name, time, charge, None, "Charged when BBMS is back", False)
            # (badge, member, time, charge, balance, a note in place of the balance, BBMS can't be reached)
            charge = "None" if s.reason else pounds(fee)
            return ("session", s.reason.upper(), s.name, time, charge, pounds(balance), None, offline)
        if self.disabled:
            return ("notice", "Reader disabled", "Ask an admin", self.qr_version, False)
        if not machine:
            if not self.hw.link_up():
                return ("notice", "No Wi-Fi", "Retrying...", self.qr_version, False)
            if offline:
                detail = self.trouble or "Retrying..."
                if self.trouble_detail:
                    detail += " " + self.trouble_detail
                return ("notice", "BBMS unavailable", detail, self.qr_version, False)
            if self.trouble:  # BBMS answers, and turns this reader away: a wrong key is a 401
                return ("notice", "Reader refused", self.trouble, self.qr_version, False)
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
