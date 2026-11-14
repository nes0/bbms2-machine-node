# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# What the reader keeps for BBMS while BBMS can't be reached: the time, and the sessions it ran alone.
#
# The time. The ESP32 has no clock that survives a power cut. Each check-in's reply carries BBMS's time,
# and from then on the reader counts on from it. Now and then that time is saved in flash, so a reader
# that starts up while BBMS is away still has a time to count from. That one is an estimate, and is
# early by however long the power was off.
#
# So a moment is written down as a "stamp": which start-up it was in, the seconds since that start-up,
# and the time as best known then. If BBMS is reached later in the same start-up, the stamp turns into
# the exact time. Otherwise the estimate is sent, marked as one.
#
# Sessions run while BBMS was away go to BBMS's "offline-events" (docs/device-api-v2.md in the BBMS
# repository) once it's back. Each has an id that never changes, so sending one twice does no harm.
#

from util import log, now_ms

MAX_EVENTS = 50  # sessions waiting to be sent; with this many waiting, nobody else can tap in offline
EVENTS_PER_SEND = 10
CLOCK_SAVE_SECONDS = 3600  # save the time to flash when the saved one is this far out


class Offline:
    def __init__(self, flash):
        self.flash = flash
        self.boot = flash.get("boots", 0) + 1
        flash.set("boots", self.boot)
        self._sequence = 0
        self._clock = flash.get("lastTime")  # the time at start-up, in unix seconds; None if never known
        self.clock_sure = False  # BBMS has given the time since start-up

        left = flash.get("offlineSession")
        if left:
            # The power went during an offline session. It ended, as near as can be told, when it was
            # last written down, which is done every minute.
            log("an offline session was cut short: recording it as ended when last seen")
            self.add_session(left["card"], left["start"], left["alive"], "device", left.get("motor", 0))

    # ---- The time -------------------------------------------------------------------------------

    def set_clock(self, unix_seconds):
        self._clock = unix_seconds - now_ms() // 1000
        self.clock_sure = True
        if abs(unix_seconds - (self.flash.get("lastTime") or 0)) > CLOCK_SAVE_SECONDS:
            self.flash.set("lastTime", unix_seconds)

    def time(self):
        """Unix seconds, or None if this reader has never been told the time."""
        return None if self._clock is None else self._clock + now_ms() // 1000

    def stamp(self, add_seconds=0):
        """This moment (or one a few seconds from it), in a form that can be kept in flash."""
        time = self.time()
        return {
            "boot": self.boot,
            "up": now_ms() // 1000 + add_seconds,
            "at": None if time is None else time + add_seconds,
            "sure": self.clock_sure,
        }

    def when(self, stamp):
        """A stamp as (unix seconds, whether that's exact and not an estimate)."""
        if stamp["boot"] == self.boot and self.clock_sure:
            return self._clock + stamp["up"], True
        if stamp["at"] is None:
            return self.time(), False
        return stamp["at"], stamp["sure"]

    # ---- A session going on now, in case the power goes ------------------------------------------

    def keep_session(self, card, name, start, motor_seconds):
        values = {
            "offlineSession": {
                "card": card,
                "name": name,
                "start": start,
                "alive": self.stamp(),
                "motor": motor_seconds,
            }
        }
        if self.time() is not None:
            values["lastTime"] = self.time()
        self.flash.update(values)

    # ---- Finished sessions waiting to be sent -----------------------------------------------------

    def events(self):
        return self.flash.get("offlineEvents", [])

    def full(self):
        return len(self.events()) >= MAX_EVENTS

    def add_session(self, card, start, end, ended_by, motor_seconds):
        self._sequence += 1
        events = self.events()
        events.append(
            {
                "id": "%d-%d" % (self.boot, self._sequence),
                "card": card,
                "start": start,
                "end": end,
                "ended_by": ended_by,
                "motor": motor_seconds,
            }
        )
        self.flash.update({"offlineEvents": events, "offlineSession": None})

    def batch(self):
        """The next events to send, as BBMS wants them."""
        body = []
        for event in self.events()[:EVENTS_PER_SEND]:
            started, sure_start = self.when(event["start"])
            ended, sure_end = self.when(event["end"])
            item = {
                "id": event["id"],
                "type": "session",
                "card": event["card"],
                "started_at": started,
                "ended_at": max(ended, started + 1),
                "ended_by": event["ended_by"],
                "motor_on_seconds": event["motor"],
            }
            if not (sure_start and sure_end):
                item["clock"] = "estimated"  # BBMS keeps what it's sent, so this can be seen later
            body.append(item)
        return body

    def sent(self, ids):
        self.flash.set("offlineEvents", [event for event in self.events() if event["id"] not in ids])
