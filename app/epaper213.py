# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# Electronic Paper Display driver
#

import asyncio
import time

import display_base
from machine import SPI, Pin


class EPD(display_base.EPD):
    MAX_LINE_BYTES = 16
    MAX_COLUMN_BYTES = 250

    X_Addr_Start = 0x01
    X_Addr_End = 0x10

    Y_Addr_Start = 249
    Y_Addr_End = 0

    spi = SPI(1, baudrate=2_000_000, polarity=0, phase=0, sck=Pin(18), mosi=Pin(23))
    cs = Pin(5, Pin.OUT)
    dc = Pin(17, Pin.OUT)
    rst = Pin(16, Pin.OUT)
    busy = Pin(4, Pin.IN)

    def reset(self):
        self.rst.value(0)
        time.sleep_ms(10)
        self.rst.value(1)
        time.sleep_ms(10)

    def send_cmd(self, cmd):
        self.cs.value(0)
        self.dc.value(0)
        time.sleep_us(5)
        self.spi.write(bytearray([cmd]))
        time.sleep_us(5)
        self.cs.value(1)

    def send_data(self, data):
        self.cs.value(0)
        self.dc.value(1)
        time.sleep_us(5)
        self.spi.write(bytearray([data]))
        time.sleep_us(5)
        self.cs.value(1)

    def wait_busy(self):
        while self.busy.value() == 1:
            time.sleep_ms(1)

    def init(self):
        self.reset()
        self.wait_busy()

        self.send_cmd(0x12)
        self.wait_busy()

        self.configure()
        self.wait_busy()

    def configure(self):
        self.send_cmd(0x01)
        self.send_data(self.Y_Addr_Start & 0xFF)
        self.send_data(self.Y_Addr_Start >> 8)
        self.send_data(0x00)

        self.send_cmd(0x11)
        self.send_data(0x01)

        self.send_cmd(0x44)
        self.send_data(self.X_Addr_Start)
        self.send_data(self.X_Addr_End)

        self.send_cmd(0x45)
        self.send_data(self.Y_Addr_Start & 0xFF)
        self.send_data(self.Y_Addr_Start >> 8)
        self.send_data(self.Y_Addr_End & 0xFF)
        self.send_data(self.Y_Addr_End >> 8)

        self.send_cmd(0x4E)
        self.send_data(self.X_Addr_Start)

        self.send_cmd(0x4F)
        self.send_data(self.Y_Addr_Start & 0xFF)
        self.send_data(self.Y_Addr_Start >> 8)

        self.send_cmd(0x3C)
        # self.send_data(0x05)
        self.send_data(0x80)

        self.send_cmd(0x21)
        self.send_data(0x00)
        self.send_data(0x00)

    def display(self):
        self.send_cmd(0x24)  # WRITE_RAM
        for b in self.buffer:
            self.send_data(b)
        # self.send_cmd(0x26)  # WRITE_RAM
        # for b in self.buffer:
        #     self.send_data(b)

        self.send_cmd(0x22)
        self.send_data(0xF7)  # display update
        self.send_cmd(0x20)
        self.wait_busy()

        self.send_cmd(0x10)
        self.send_data(0x01)
        time.sleep_ms(100)

    async def wait_busy_async(self, timeout_ms=10000):
        """Wait for the panel without holding up other tasks. False if it's still busy after the timeout."""
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self.busy.value() == 1:
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                return False
            await asyncio.sleep_ms(10)
        return True

    async def display_async(self):
        """
        The same full update as display(), for a program that has other things to do meanwhile.
        display() leaves the panel in deep sleep, which only a reset ends, so this starts with one.
        """
        self.reset()
        await self.wait_busy_async()
        self.send_cmd(0x12)
        await self.wait_busy_async()
        self.configure()
        await self.wait_busy_async()

        self.send_cmd(0x24)  # WRITE_RAM: the whole buffer in one go
        self.cs.value(0)
        self.dc.value(1)
        self.spi.write(self.buffer)
        self.cs.value(1)

        self.send_cmd(0x22)
        self.send_data(0xF7)  # display update
        self.send_cmd(0x20)
        finished = await self.wait_busy_async()

        self.send_cmd(0x10)  # deep sleep
        self.send_data(0x01)
        return finished
