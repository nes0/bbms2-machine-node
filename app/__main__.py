# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# Entry point for simulation mode: the reader program (reader.py) with pretend hardware, in a terminal.
# Run it from the folder above with `python app`; it reads data/config.json like the real reader.
#

import asyncio

import config
from bbms_api import Api
from flash import Flash
from reader import Reader
from screen import Screen
from simulation import EPD
from simulation.sim_hardware import SimHardware
from textual.app import App
from textual.containers import Container
from textual.widgets import Button, Input, Label, Switch


class DisplayApp(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 1;
        grid-rows: auto auto auto 1fr;
    }
    BrailleDisplay {
        padding: 0;
        border: round yellow;
        border-title-align: left;
        width: 127;
        height: 32;
    }
    Input {
        width: 30;
    }
    #amps {
        width: 10;
    }
    #buttons, #bench {
        layout: grid;
        grid-size: 7;
        grid-gutter: 2;
        grid-columns: auto auto auto auto auto auto 1fr;
        height: auto;
    }
    #bench Label, #buttons Label {
        padding: 1 0;
    }
    """

    def compose(self):
        self.epd = EPD()
        self.display2 = self.epd.widget
        epd_container = Container(self.display2, id="epd")
        epd_container.border_title = "[i]E-paper display[/i]"
        yield epd_container
        with Container(id="buttons"):
            yield Label("Fob:")
            yield Input(placeholder="card number or 12-hex tag", id="fob")
            yield Button("Tap Fob", id="tap", variant="primary")
            yield Button("STOP", id="stop", variant="error")
            yield Button("FAULT", id="fault", variant="warning")
            yield Label("Relay: off", id="relay")
        with Container(id="bench"):
            yield Label("Motor running:")
            yield Switch(id="motor")
            yield Input(value="5", placeholder="amps", id="amps", type="number")
            yield Label("Network:")
            yield Switch(value=True, id="network")
            yield Label("", id="status")

    def on_mount(self):
        settings, problems = config.load()
        self.hw = SimHardware(self.epd, on_relay=self.show_relay)
        screen = Screen(self.epd)
        if problems:
            screen.render(("message", ("Not set up",) + tuple(problems)))
            self.epd.display()
            return
        api = Api(
            settings["api_url"],
            settings["api_key"],
            ca_file=settings["ca_file"],
            tls_insecure=settings["tls_insecure"],
            timeout=settings["request_timeout_seconds"],
            link_up=self.hw.link_up,
        )
        self.reader = Reader(self.hw, api, Flash(config.DATA_DIR), screen)
        self.reader_task = asyncio.create_task(self.reader.run())

    def show_relay(self, on):
        self.query_one("#relay", Label).update("Relay: ON" if on else "Relay: off")

    def tap(self):
        card = self.query_one("#fob", Input).value.strip()
        try:
            self.hw.tap(card)
            self.query_one("#status", Label).update("")
        except ValueError:
            self.query_one("#status", Label).update("A fob is a card number (digits) or a 12-character hex tag.")

    def on_button_pressed(self, event):
        if event.button.id == "tap":
            self.tap()
        elif event.button.id == "stop":
            self.hw.press_stop()
        elif event.button.id == "fault":
            self.hw.press_fault()

    def on_input_submitted(self, event):
        if event.input.id == "fob":
            self.tap()

    def on_input_changed(self, event):
        if event.input.id == "amps":
            self.set_current()

    def set_current(self):
        """What the machine draws when it has power: the amps box while the motor switch is on."""
        try:
            amps = max(0.0, float(self.query_one("#amps", Input).value))
        except ValueError:
            amps = 0.0
        self.hw.current = amps if self.query_one("#motor", Switch).value else 0.0

    def on_switch_changed(self, event):
        if event.switch.id == "motor":
            self.set_current()
        elif event.switch.id == "network":
            self.hw.network = event.value


if __name__ == "__main__":
    DisplayApp().run()
