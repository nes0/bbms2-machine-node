# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# The ESP32's side of the reader: relay, buttons, fob reader, sounder, current sensor, Wi-Fi and watchdog.
# The pin numbers come from config.py. (simulation/sim_hardware.py is the pretend version of this.)
#

import time

import network
from machine import ADC, PWM, UART, WDT, Pin, reset
from rdm6300 import Rdm6300

WIFI_RETRY_MS = 30000
ZERO_SETTLE_MS = 5000  # after the relay opens, the current sensor's filter takes a few seconds to fall to zero

# What the sounder plays: (frequency in Hz, milliseconds) for each note. The reader does nothing else
# while one plays, so they are short.
CHIRP_FOB = ((2700, 60),)  # a fob has been read
CHIRP_ON = ((1800, 70), (2700, 100))  # the machine has been switched on: rising
CHIRP_OFF = ((2700, 70), (1800, 100))  # and off: falling


class Button:
    """A push button between a pin and ground. Hardware.poll() calls poll() every 30 ms or so."""

    def __init__(self, pin_number):
        self.pin = Pin(pin_number, Pin.IN, Pin.PULL_UP)
        self._lows = 0
        self._armed = False  # a button held down at start-up isn't a press
        self._pressed = False

    def poll(self):
        if self.pin.value():
            self._lows = 0
            self._armed = True
        elif self._lows < 2:
            # Down for two looks running is a press; one alone could be contact bounce or interference.
            self._lows += 1
            if self._lows == 2 and self._armed:
                self._armed = False
                self._pressed = True

    def take(self):
        """True once for each press."""
        pressed = self._pressed
        self._pressed = False
        return pressed


class Hardware:
    def __init__(self, settings):
        # The relay first, and off, before anything that could fail.
        self._relay_on_level = 1 if settings["relay_active_high"] else 0
        self._relay = Pin(settings["pin_relay"], Pin.OUT, value=1 - self._relay_on_level)

        self._buzzer = None
        if settings["pin_buzzer"] is not None:
            try:
                self._buzzer = PWM(Pin(settings["pin_buzzer"]), freq=CHIRP_FOB[0][0], duty_u16=0)
            except Exception as error:  # the reader works without it
                print("buzzer:", repr(error))

        self._watchdog = None
        if settings["watchdog_seconds"]:
            self._watchdog = WDT(timeout=settings["watchdog_seconds"] * 1000)

        self._stop = Button(settings["pin_stop_button"])
        self._fault = Button(settings["pin_fault_button"])

        # UART 2's usual pins (16 and 17) are kept free, so both pins are always named here.
        uart = UART(2, baudrate=9600, bits=8, parity=None, stop=1, tx=settings["pin_rfid_tx"],
                    rx=settings["pin_rfid_rx"], rxbuf=256, timeout=0)  # fmt: skip
        self._rfid = Rdm6300(uart)

        # 11 dB attenuation: the ADC then reads from about 0.15 V up to about 2.5 V.
        self._adc = ADC(Pin(settings["pin_current_sensor"]), atten=ADC.ATTN_11DB)
        self._mv_per_amp = settings["current_mv_per_amp"]
        self._zero_nominal_mv = settings["current_zero_mv"]
        self._zero_tolerance_mv = settings["current_zero_tolerance_mv"]
        self._zero_mv = self._zero_nominal_mv  # until it has been measured
        self._zero_fault = False
        self._relay_on = False
        self._relay_off_since = time.ticks_ms()

        self._ssid = settings["wifi_ssid"]
        self._password = settings["wifi_password"]
        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)
        self._wifi_tried = time.ticks_ms()
        self._wifi_connect()

        from epaper213 import EPD

        self.epd = EPD(settings)

    # ---- What reader.py uses ----------------------------------------------------------------------

    def set_relay(self, on):
        changed = on != self._relay_on
        if self._relay_on and not on:
            self._relay_off_since = time.ticks_ms()
        self._relay_on = on
        self._relay.value(self._relay_on_level if on else 1 - self._relay_on_level)
        if changed:
            self.chirp(CHIRP_ON if on else CHIRP_OFF)

    def chirp(self, notes):
        """Play a few notes on the sounder, if there is one. Returns when they have finished."""
        buzzer = self._buzzer
        if not buzzer:
            return
        try:
            for frequency, milliseconds in notes:
                buzzer.freq(frequency)
                buzzer.duty_u16(32768)
                time.sleep_ms(milliseconds)
        except Exception as error:
            print("buzzer:", repr(error))
        finally:
            buzzer.duty_u16(0)

    def amps(self):
        """Roughly the machine's current. Good enough to tell "motor running" from "motor stopped"."""
        microvolts = 0
        for _ in range(8):
            microvolts += self._adc.read_uv()
        millivolts = microvolts / 8000
        if not self._relay_on and time.ticks_diff(time.ticks_ms(), self._relay_off_since) > ZERO_SETTLE_MS:
            self._measure_zero(millivolts)
        return max(0, millivolts - self._zero_mv) / self._mv_per_amp

    def _measure_zero(self, millivolts):
        """The relay has been off for a while, so the machine draws nothing: this is what zero reads."""
        if abs(millivolts - self._zero_nominal_mv) > self._zero_tolerance_mv:
            if not self._zero_fault:  # say so once, not every second
                print("current sensor: reads %d mV with the relay off; expected about %d mV. Not used as zero."
                      % (millivolts, self._zero_nominal_mv))  # fmt: skip
            self._zero_fault = True
            return
        self._zero_fault = False
        self._zero_mv += (millivolts - self._zero_mv) / 4  # smoothed: one reading has some noise on it

    def poll(self, now):
        """Look at the buttons and the fob reader. Called every 30 ms or so."""
        self._stop.poll()
        self._fault.poll()
        if self._rfid.poll(now):
            self.chirp(CHIRP_FOB)
        if not self._wlan.isconnected() and time.ticks_diff(time.ticks_ms(), self._wifi_tried) > WIFI_RETRY_MS:
            self._wifi_connect()  # the ESP32 reconnects by itself; this is for when it has given up

    def take_card(self, now):
        return self._rfid.take(now)

    def take_stop(self):
        return self._stop.take()

    def take_fault(self):
        return self._fault.take()

    def discard_inputs(self):
        self._rfid.discard()
        self._stop.take()
        self._fault.take()

    def link_up(self):
        return self._wlan.isconnected()

    def feed_watchdog(self):
        if self._watchdog:
            self._watchdog.feed()

    def reboot(self):
        self.set_relay(False)
        reset()

    # ---------------------------------------------------------------------------------------------

    def _wifi_connect(self):
        self._wifi_tried = time.ticks_ms()
        if not self._ssid:
            return
        try:
            self._wlan.disconnect()
        except OSError:
            pass  # it wasn't connected
        try:
            self._wlan.connect(self._ssid, self._password)
        except OSError as error:
            print("wifi:", error)
