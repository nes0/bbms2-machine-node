"""
The reader when BBMS can't be reached, tested on a computer: the fob list (app/fobs.py), what is kept to
send later (app/offline.py), the HTTP client's view of each kind of failure (app/bbms_api.py) and the
reader's own judgement (app/reader.py). Run with `python -m unittest discover tests`.

The reader runs against a pretend BBMS and pretend hardware, with a pretend clock so that an hour's
outage takes no time. The fob is the made-up one from BBMS's documents: tag 700012D68733, card 0001234567.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import bbms_api  # noqa: E402
import fobs  # noqa: E402
import offline  # noqa: E402
import reader as reader_module  # noqa: E402
from bbms_api import Api, Result  # noqa: E402
from flash import Flash  # noqa: E402

KIM = "700012D68733"  # on the fob list
SAM = "70000003E89B"  # the right shape for a fob, but not on the list
START = 1793910000  # BBMS's clock when a test begins, in unix seconds


class Clock:
    """The reader's milliseconds since start-up, moved on by the tests."""

    def __init__(self):
        self.ms = 0
        self.world_ms = 0  # never goes back, unlike `ms` when the reader restarts

    def now_ms(self):
        return self.ms

    def advance(self, ms):
        self.ms += ms
        self.world_ms += ms


class FakeEpd:
    async def display_async(self):
        return True


class FakeScreen:
    def __init__(self):
        self.view = None

    def render(self, view, qr=None):
        self.view = view


class FakeHardware:
    def __init__(self):
        self.epd = FakeEpd()
        self.relay = False
        self.current = 0.0
        self.network = True
        self._card = None
        self._stop = False
        self._fault = False

    def tap(self, card):
        self._card = card

    def press_stop(self):
        self._stop = True

    def press_fault(self):
        self._fault = True

    def set_relay(self, on):
        self.relay = on

    def amps(self):
        return self.current if self.relay else 0.0

    def poll(self, now):
        pass

    def take_card(self, now):
        card, self._card = self._card, None
        return card

    def take_stop(self):
        pressed, self._stop = self._stop, False
        return pressed

    def take_fault(self):
        pressed, self._fault = self._fault, False
        return pressed

    def discard_inputs(self):
        self._card = None
        self._stop = self._fault = False

    def link_up(self):
        return self.network

    def feed_watchdog(self):
        pass

    def reboot(self):
        raise SystemExit("reboot")


class FakeBbms:
    """Stands in for bbms_api.Api and for BBMS behind it. `fault` says how requests fail, if they do."""

    FAULTS = {
        "no answer": lambda: Result(error="wait: timed out", why="No answer"),
        "no dns": lambda: Result(error="dns: OSError(-202)", why="Server not found"),
        "no wifi": lambda: Result(error="no network", why="No Wi-Fi"),
        "certificate": lambda: Result(error="connect: CERTIFICATE_VERIFY_FAILED", why="Certificate problem"),
        "502": lambda: Result(502),
        "503": lambda: Result(503),
        "login page": lambda: Result(200),  # a 200 that isn't JSON: something between the reader and BBMS
        "wrong key": lambda: Result(401),
    }

    def __init__(self, clock):
        self.clock = clock
        self.key = "a-device-key"
        self.host = "bbms.example"
        self.address = None
        self.fault = None
        self.requests = []  # (method, path, body) of everything that got through
        self.attempts = []  # paths of everything tried, whether or not it got through
        self.fob_list = [
            {"card": "0001234567", "tag": KIM, "display_name": "Kim K."},
            {"card": "0000000042", "tag": None, "display_name": 'Lee {the} "Lathe" O\'Neil'},
        ]
        self.cache_version = "9f2c41d0"
        self.machine = {
            "name": "Red laser",
            "config_version": 1,
            "hourly_rate_pence": 300,
            "minimum_minutes": 15,
            "status": "working",
            "status_note": "",
            "status_since": START,
            "in_use_by": None,
            "session_id": None,
            "meter": {"mode": "session", "current_threshold_amps": None, "report_current": False},
        }
        self.commands = []
        self.next_session = 100
        self.open_session = None
        self.ends = []  # the bodies of session ends
        self.events = []  # offline events received
        self.reported_cache_version = "never"

    def time(self):
        return START + self.clock.world_ms // 1000

    def machine_now(self):
        machine = json.loads(json.dumps(self.machine))
        machine["session_id"] = self.open_session
        machine["in_use_by"] = "Kim K." if self.open_session else None
        return machine

    def tidy_up(self):
        """What BBMS does to a session it hasn't heard from for an hour."""
        self.open_session = None

    async def request(self, method, path, body=None, etag=None, binary=False):
        self.attempts.append(path)
        if self.fault:
            return self.FAULTS[self.fault]()
        self.requests.append((method, path, body))
        if path == "qr":
            return Result(200, bytes([1, 0x80]), '"qr1"')
        if path == "check-in":
            self.reported_cache_version = body["cache_version"]
            commands, self.commands = list(self.commands), []
            if body["cache_version"] != self.cache_version:
                commands.append("refresh_cache")
            return Result(
                200,
                {
                    "server_time": self.time(),
                    "heartbeat_seconds": 300,
                    "session_heartbeat_seconds": 60,
                    "cache_version": self.cache_version,
                    "commands": commands,
                    "machine": self.machine_now(),
                },
            )
        if path == "access":
            if not any(body["card"] == fob["tag"] for fob in self.fob_list):
                return Result(200, {"allowed": False, "reason": "unknown_fob", "machine": self.machine_now()})
            self.next_session += 1
            self.open_session = self.next_session
            return Result(
                200,
                {
                    "allowed": True,
                    "session_id": self.open_session,
                    "display_name": "Kim K.",
                    "balance_pence": 1000,
                    "machine": self.machine_now(),
                },
            )
        if path.endswith("/heartbeat"):
            if self.open_session != int(path.split("/")[1]):
                return Result(200, {"open": False, "ended_by": "tidy-up", "duration_seconds": 600, "fee_pence": 75})
            return Result(200, {"open": True, "elapsed_seconds": 60, "fee_so_far_pence": 75, "balance_pence": 925})
        if path.endswith("/end"):
            self.ends.append(body)
            self.open_session = None
            return Result(200, {"ended": True, "duration_seconds": 600, "fee_pence": 75, "machine": self.machine_now()})
        if path == "offline-events":
            self.events.extend(body["events"])
            return Result(200, {"results": [{"id": e["id"], "result": "recorded"} for e in body["events"]]})
        return Result(404)

    async def download(self, path, save_as, timeout, max_bytes=None):
        self.attempts.append(path)
        if self.fault:
            return self.FAULTS[self.fault]()
        assert path == "allowed-fobs"
        self.requests.append(("GET", path, None))
        with open(save_as, "w") as f:
            json.dump({"cache_version": self.cache_version, "fobs": self.fob_list}, f)
        return Result(200, (0, ""))


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self._real = (reader_module.now_ms, offline.now_ms)
        reader_module.now_ms = offline.now_ms = self.clock.now_ms
        self._log = (reader_module.log, offline.log)
        reader_module.log = offline.log = lambda *args: None
        self.bbms = FakeBbms(self.clock)
        self.loop = asyncio.new_event_loop()
        self.start()

    def tearDown(self):
        reader_module.now_ms, offline.now_ms = self._real
        reader_module.log, offline.log = self._log
        self.loop.close()
        self.folder.cleanup()

    def start(self, offline_use=True):
        """Switch the reader on (again: what was in memory is gone, what was in flash is still there)."""
        self.clock.ms = 0
        self.hw = FakeHardware()
        self.screen = FakeScreen()
        self.flash = Flash(self.folder.name)
        self.reader = reader_module.Reader(self.hw, self.bbms, self.flash, self.screen, None, offline_use)
        self.loop.run_until_complete(self.reader.load_qr())
        self.loop.run_until_complete(self.reader.check_in())
        self.run_for(1)

    def run_for(self, seconds, step_ms=250):
        async def steps():
            for _ in range(int(seconds * 1000 / step_ms)):
                self.clock.advance(step_ms)
                # What Reader._watch_current does once a second.
                reader = self.reader
                if reader.session and reader.metering_by_current() and self.hw.amps() > (reader.threshold or 0):
                    reader.session.motor_ms += step_ms
                await reader.step()

        self.loop.run_until_complete(steps())

    def tap(self, card=KIM):
        self.hw.tap(card)
        self.run_for(1)

    def message(self):
        return self.screen.view[1] if self.screen.view[0] == "message" else None

    def paths(self, since=0):
        return [path for _, path, _ in self.bbms.requests[since:]]


class FobListFetching(ReaderCase):
    def test_the_list_is_fetched_and_its_version_reported(self):
        self.assertIn("allowed-fobs", self.paths())
        self.assertEqual(self.flash.get("cacheVersion"), "9f2c41d0")
        self.assertEqual(self.reader.fobs.find(KIM), "Kim K.")
        self.run_for(300)
        self.assertEqual(self.bbms.reported_cache_version, "9f2c41d0")
        self.assertEqual(self.paths().count("allowed-fobs"), 1)  # not fetched again while it's current

    def test_a_changed_list_is_fetched_again(self):
        self.bbms.fob_list = self.bbms.fob_list[1:]
        self.bbms.cache_version = "00000002"
        self.run_for(301)
        self.assertEqual(self.flash.get("cacheVersion"), "00000002")
        self.assertIsNone(self.reader.fobs.find(KIM))

    def test_purge_empties_the_list(self):
        self.bbms.commands = ["purge_cache"]
        real = self.bbms.download

        async def broken(path, save_as, timeout, max_bytes=None):
            return Result(503)

        self.bbms.download = broken  # so that the list isn't fetched again straight afterwards
        self.run_for(301)
        self.assertIsNone(self.flash.get("cacheVersion"))
        self.assertIsNone(self.reader.fobs.find(KIM))
        self.bbms.download = real
        self.run_for(300)  # the next check-in says there's nothing held, and BBMS says to fetch it
        self.assertEqual(self.reader.fobs.find(KIM), "Kim K.")

    def test_a_failed_fetch_keeps_the_old_list(self):
        self.bbms.cache_version = "00000002"
        real = self.bbms.download

        async def broken(path, save_as, timeout, max_bytes=None):
            with open(save_as, "w") as f:
                f.write('{"cache_version": "00000002", "fobs": [{"card": "00012')  # the connection dropped
            return Result(200, (0, ""))

        self.bbms.download = broken
        self.run_for(301)
        self.assertEqual(self.flash.get("cacheVersion"), "9f2c41d0")
        self.assertEqual(self.reader.fobs.find(KIM), "Kim K.")
        self.bbms.download = real
        self.run_for(300)
        self.assertEqual(self.flash.get("cacheVersion"), "00000002")


class WithoutBbms(ReaderCase):
    def test_every_kind_of_failure_lets_a_listed_member_in(self):
        for fault in ("no answer", "no dns", "no wifi", "certificate", "502", "503", "login page"):
            with self.subTest(fault):
                self.bbms.fault = fault
                self.tap()
                self.assertTrue(self.hw.relay)
                self.assertEqual(self.screen.view[0], "session")
                self.assertEqual(self.screen.view[2:4], ("OFFLINE", "Kim K."))
                self.tap()
                self.assertFalse(self.hw.relay)
                self.bbms.fault = None
                self.run_for(120)  # BBMS is back: everything is sent
                self.assertEqual(self.flash.get("offlineEvents"), [])

    def test_a_fob_not_on_the_list_is_refused(self):
        self.bbms.fault = "502"
        self.tap(SAM)
        self.assertFalse(self.hw.relay)
        self.assertEqual(self.message(), ("Can't reach BBMS", "Server error 502", "Fob not on the offline list"))

    def test_a_no_from_bbms_is_not_overruled(self):
        self.bbms.fault = "wrong key"
        self.tap()
        self.assertFalse(self.hw.relay)
        self.assertEqual(self.message(), ("BBMS turned the", "reader away (401)"))

    def test_a_machine_out_of_order_stays_off(self):
        self.bbms.machine["status"] = "out_of_order"
        self.run_for(301)
        self.bbms.fault = "no answer"
        self.tap()
        self.assertFalse(self.hw.relay)
        self.assertEqual(self.message(), ("Out of order",))

    def test_a_disabled_reader_stays_disabled_through_a_restart(self):
        self.bbms.commands = ["disable"]
        self.run_for(301)
        self.bbms.fault = "no answer"
        self.start()
        self.tap()
        self.assertFalse(self.hw.relay)

    def test_offline_use_can_be_switched_off(self):
        self.start(offline_use=False)
        self.bbms.fault = "no answer"
        self.tap()
        self.assertFalse(self.hw.relay)
        self.assertEqual(self.message(), ("Can't reach BBMS", "No answer", "Try again shortly"))

    def test_the_session_is_reported_with_its_times(self):
        self.bbms.machine["meter"] = {"mode": "current", "current_threshold_amps": "2.5", "report_current": False}
        self.run_for(300)
        self.bbms.fault = "no answer"
        self.run_for(100)
        started = self.bbms.time()
        self.tap()
        self.hw.current = 10
        self.run_for(40 * 60)
        self.hw.current = 0
        self.run_for(5 * 60)
        self.hw.press_stop()
        self.run_for(1)
        ended = self.bbms.time()
        self.assertEqual(self.message()[1:], ("BBMS will be told", "when it's back"))
        self.run_for(3600)
        self.assertEqual(self.bbms.events, [])
        self.bbms.fault = None
        self.run_for(120)
        self.assertEqual(len(self.bbms.events), 1)
        event = self.bbms.events[0]
        self.assertEqual((event["type"], event["card"], event["ended_by"]), ("session", KIM, "button"))
        self.assertAlmostEqual(event["started_at"], started, delta=2)
        self.assertAlmostEqual(event["ended_at"], ended, delta=2)
        self.assertAlmostEqual(event["motor_on_seconds"], 40 * 60, delta=2)
        self.assertNotIn("clock", event)  # the times are exact

    def test_someone_elses_fob_does_not_end_an_offline_session(self):
        self.bbms.fault = "no answer"
        self.tap()
        self.tap(SAM)
        self.assertTrue(self.hw.relay)
        self.assertEqual(self.message(), ("In use by", "Kim K.", "Press Stop to end"))

    def test_only_check_in_is_tried_while_bbms_is_away(self):
        self.tap()  # a session BBMS knows about
        self.assertTrue(self.hw.relay)
        self.bbms.fault = "no answer"
        self.run_for(120)
        before = len(self.bbms.attempts)
        self.run_for(600)
        tried = self.bbms.attempts[before:]
        self.assertEqual(set(tried), {"check-in"})
        self.assertLessEqual(len(tried), 11)  # once a minute
        self.assertTrue(self.hw.relay)  # and the session carries on

    def test_a_tap_while_away_does_not_wait_for_bbms(self):
        self.bbms.fault = "no answer"
        self.run_for(301)  # a check-in has failed, so the reader knows
        before = len(self.bbms.attempts)
        self.hw.tap(KIM)
        self.run_for(0.25)
        self.assertTrue(self.hw.relay)
        self.assertEqual(self.bbms.attempts[before:], [])

    def test_starting_up_without_bbms(self):
        self.bbms.fault = "no wifi"
        self.clock.world_ms += 7200000  # the power was off for two hours
        self.start()
        self.assertEqual(self.screen.view[0:2], ("ready", "Red laser"))
        self.tap()
        self.assertTrue(self.hw.relay)
        self.run_for(600)
        ended = self.bbms.time()
        self.tap()
        self.bbms.fault = None
        self.run_for(120)
        # BBMS came back before the reader was switched off again, so the times could be put right.
        event = self.bbms.events[0]
        self.assertNotIn("clock", event)
        self.assertAlmostEqual(event["ended_at"], ended, delta=2)
        self.assertAlmostEqual(event["ended_at"] - event["started_at"], 600, delta=2)

    def test_times_are_estimates_if_the_reader_never_learnt_the_time(self):
        self.bbms.fault = "no wifi"
        self.clock.world_ms += 7200000  # the power was off for two hours
        self.start()
        self.tap()
        self.run_for(600)
        self.tap()
        self.clock.world_ms += 7200000  # and again, before BBMS came back
        self.start()
        self.bbms.fault = None
        self.run_for(120)
        event = self.bbms.events[0]
        self.assertEqual(event["clock"], "estimated")
        self.assertAlmostEqual(event["ended_at"] - event["started_at"], 600, delta=2)  # the length is still right
        self.assertLess(event["ended_at"], self.bbms.time())  # and the estimate is early, never in the future

    def test_a_power_cut_during_an_offline_session(self):
        self.bbms.fault = "no answer"
        started = self.bbms.time()
        self.tap()
        self.run_for(10 * 60 + 20)
        self.start()  # the power went and came back
        self.assertFalse(self.hw.relay)
        self.bbms.fault = None
        self.run_for(120)
        event = self.bbms.events[0]
        self.assertEqual(event["ended_by"], "device")
        self.assertAlmostEqual(event["started_at"], started, delta=3)
        self.assertAlmostEqual(event["ended_at"] - event["started_at"], 600, delta=61)

    def test_events_are_kept_until_bbms_answers_for_them(self):
        self.bbms.fault = "no answer"
        self.tap()
        self.run_for(120)
        self.tap()
        self.bbms.fault = "502"
        self.run_for(600)
        self.start()
        self.assertEqual(len(self.flash.get("offlineEvents")), 1)
        self.bbms.fault = None
        self.run_for(120)
        self.assertEqual(len(self.bbms.events), 1)
        self.assertEqual(self.flash.get("offlineEvents"), [])

    def test_the_offline_record_can_fill_up(self):
        self.bbms.fault = "no answer"
        for _ in range(offline.MAX_EVENTS):
            self.tap()
            self.run_for(5)
            self.tap()
            self.run_for(10)
        self.tap()
        self.assertFalse(self.hw.relay)
        self.assertEqual(self.message()[2], "Offline record is full")
        self.bbms.fault = None
        self.run_for(300)
        self.assertEqual(len(self.bbms.events), offline.MAX_EVENTS)
        self.assertEqual(len({event["id"] for event in self.bbms.events}), offline.MAX_EVENTS)


class SessionsBbmsStarted(ReaderCase):
    def test_an_end_while_away_is_sent_later_with_its_time(self):
        self.tap()
        self.run_for(300)
        self.bbms.fault = "no answer"
        self.run_for(300)
        ended = self.bbms.time()
        self.tap()
        self.assertFalse(self.hw.relay)
        self.run_for(1200)
        self.bbms.fault = None
        self.run_for(120)
        self.assertEqual(len(self.bbms.ends), 1)
        self.assertEqual(self.bbms.ends[0]["card"], KIM)
        self.assertAlmostEqual(self.bbms.ends[0]["ended_at"], ended, delta=2)
        self.assertIsNone(self.flash.get("pendingEnd"))

    def test_an_end_waiting_to_be_sent_survives_a_restart(self):
        self.tap()
        self.run_for(300)
        self.bbms.fault = "no answer"
        ended = self.bbms.time()
        self.hw.press_stop()
        self.run_for(60)
        self.start()
        self.bbms.fault = None
        self.run_for(120)
        self.assertEqual(self.bbms.ends[0]["ended_by"], "button")
        self.assertAlmostEqual(self.bbms.ends[0]["ended_at"], ended, delta=2)

    def test_a_session_bbms_gave_up_on_carries_on_offline(self):
        self.tap()
        self.run_for(300)
        last_heard = self.bbms.time()
        self.bbms.fault = "no answer"
        self.run_for(2 * 3600)
        self.bbms.tidy_up()
        self.bbms.fault = None
        self.run_for(120)
        self.assertTrue(self.hw.relay)  # the member wasn't cut off
        self.assertEqual(self.screen.view[2], "OFFLINE")
        self.tap()
        self.assertFalse(self.hw.relay)
        ended = self.bbms.time()
        self.run_for(60)
        event = self.bbms.events[0]
        # It starts just after the last heartbeat BBMS answered, which is where BBMS ended its own.
        self.assertGreater(event["started_at"], last_heard - 60)
        self.assertLessEqual(event["started_at"], last_heard + reader_module.TAKE_OVER_SECONDS + 2)
        self.assertAlmostEqual(event["ended_at"], ended, delta=2)

    def test_a_session_bbms_ended_on_purpose_still_ends(self):
        self.tap()
        self.run_for(200)
        self.bbms.tidy_up()  # or an admin: the reader was in touch all along
        self.run_for(120)
        self.assertFalse(self.hw.relay)


class FobListFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "fobs.txt")

    def tearDown(self):
        self.folder.cleanup()

    def store(self, reply):
        with open(self.path + ".json", "wb") as f:
            f.write(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
        return fobs.FobList(self.path).store(self.path + ".json")

    def test_awkward_names_and_many_entries(self):
        names = ["Kim K.", 'Bob "{Braces}" [B].', "Zoë \\ Ω.", "a\tb\nc", ""]
        entries = [
            {"card": "%010d" % n, "tag": None if n % 3 == 0 else "70%08X00" % n, "display_name": names[n % len(names)]}
            for n in range(1, 700)
        ]
        self.assertEqual(self.store({"cache_version": "abc", "fobs": entries}), ("abc", 699))
        held = fobs.FobList(self.path)
        self.assertEqual(held.find("70%08X00" % 1), 'Bob "{Braces}" [B].')
        self.assertEqual(held.find("70%08X00" % 2), "Zoë \\ Ω.")
        self.assertEqual(held.find("70%08X00" % 3), "a b c")  # found by its card number: BBMS holds no tag for it
        self.assertEqual(held.find("71%08X00" % 4), "")  # another vendor prefix, the same card number
        self.assertEqual(held.find("70%08x00" % 699), names[699 % 5])
        self.assertIsNone(held.find("70%08X00" % 700))
        self.assertFalse(os.path.exists(self.path + ".json"))

    def test_an_empty_list(self):
        self.assertEqual(self.store({"cache_version": "e3b0c442", "fobs": []}), ("e3b0c442", 0))
        self.assertIsNone(fobs.FobList(self.path).find(KIM))

    def test_a_bad_reply_leaves_the_old_list(self):
        self.store({"cache_version": "abc", "fobs": [{"card": "0001234567", "tag": KIM, "display_name": "Kim K."}]})
        for bad in (
            b"<html>Log in to the Wi-Fi</html>",
            b'{"cache_version": "x", "fobs": [{"card": "1"',
            b'{"fobs": []}',
        ):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    self.store(bad)
                self.assertEqual(fobs.FobList(self.path).find(KIM), "Kim K.")
                self.assertEqual(os.listdir(self.folder.name), ["fobs.txt"])

    def test_scan_gives_back_the_rest_of_the_reply(self):
        seen = []
        reply = b'{"cache_version": "v", "fobs": [{"a": {"b": [1, {"c": 2}]}}, {"d": "}]"}], "more": [1, 2]}'
        outer = fobs.scan(io.BytesIO(reply), seen.append)
        self.assertEqual(seen, [{"a": {"b": [1, {"c": 2}]}}, {"d": "}]"}])
        self.assertEqual(outer["cache_version"], "v")
        self.assertEqual(outer["fobs"], [])


class HttpFailures(unittest.TestCase):
    """bbms_api.Api against real sockets on this computer: how each kind of failure comes out."""

    def setUp(self):
        self._log = bbms_api.log
        bbms_api.log = lambda *args: None

    def tearDown(self):
        bbms_api.log = self._log

    def ask(self, reply=None, host="127.0.0.1", address=None, hang=False, timeout=2):
        """One request to a server on this computer that sends `reply` (None: nothing is listening)."""

        async def go():
            seen = []

            async def serve(reader, writer):
                seen.append(await reader.readline())
                if hang:
                    await asyncio.sleep(5)
                writer.write(reply)
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            if reply is None:
                server.close()
                await server.wait_closed()
            api = Api("http://%s:%d/api/devices/v2/" % (host, port), "key", timeout=timeout)
            api.address = address
            result = await api.request("POST", "check-in", {})
            server.close()
            return result, api, seen

        return asyncio.run(go())

    def test_an_answer(self):
        result, api, _ = self.ask(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{"ok": true}')
        self.assertTrue(result.ok)
        self.assertFalse(result.cut_off)
        self.assertIsNone(result.why)
        self.assertEqual(api.address, "127.0.0.1")

    def test_a_proxy_error(self):
        result, api, _ = self.ask(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "Server error 502")
        self.assertIsNone(api.address)  # that wasn't BBMS answering

    def test_a_refusal_is_an_answer(self):
        result, _, _ = self.ask(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}")
        self.assertFalse(result.ok)
        self.assertFalse(result.cut_off)
        self.assertEqual(result.why, "BBMS said no (401)")

    def test_a_page_that_is_not_bbms(self):
        result, _, _ = self.ask(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n<html>")
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "Unexpected answer")

    def test_nothing_listening(self):
        result, _, _ = self.ask(None)
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "Can't connect")

    def test_no_reply(self):
        result, _, _ = self.ask(b"", hang=True, timeout=0.3)
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "No answer")

    def test_a_name_that_cannot_be_looked_up(self):
        result, _, _ = self.ask(b"", host="bbms.invalid")
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "Server not found")

    def test_the_last_address_is_used_when_the_name_cannot_be_looked_up(self):
        reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        result, _, seen = self.ask(reply, host="bbms.invalid", address="127.0.0.1")
        self.assertTrue(result.ok)
        self.assertEqual(len(seen), 1)

    def test_failures_of_the_secure_connection_are_told_apart(self):
        # The errors as MicroPython on the ESP32 gives them.
        cases = {
            "OSError(-17040, 'MBEDTLS_ERR_RSA_PUBLIC_FAILED+MBEDTLS_ERR_MPI_ALLOC_FAILED')": "Out of memory",
            "MemoryError('memory allocation failed, allocating 16717 bytes')": "Out of memory",
            "OSError(-9984, 'MBEDTLS_ERR_X509_CERT_VERIFY_FAILED')": "Certificate problem",
            "OSError(-30592, 'MBEDTLS_ERR_SSL_FATAL_ALERT_MESSAGE')": "Secure link failed (-30592)",
            "OSError(-29312, 'MBEDTLS_ERR_SSL_CONN_EOF')": "Secure link failed (-29312)",
        }
        for error, why in cases.items():
            with self.subTest(error):

                class Shown(Exception):
                    def __repr__(self):
                        return error

                class Failing(Api):
                    async def _exchange(self, *args):
                        self._stage = "wait"
                        raise Shown()

                async def go():
                    return await Failing("https://bbms.example/", "key").request("POST", "check-in", {})

                result = asyncio.run(go())
                self.assertTrue(result.cut_off)
                self.assertEqual(result.why, why)

    def test_no_wifi(self):
        async def go():
            api = Api("http://127.0.0.1:1/", "key", link_up=lambda: False)
            return await api.request("GET", "qr")

        result = asyncio.run(go())
        self.assertTrue(result.cut_off)
        self.assertEqual(result.why, "No Wi-Fi")


if __name__ == "__main__":
    unittest.main()
