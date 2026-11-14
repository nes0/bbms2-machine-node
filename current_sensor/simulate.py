# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "matplotlib"]
# ///
"""
A simulation of the reader's current-sensor circuit. Run it with `uv run current_sensor/simulate.py`;
it prints its findings and saves graphs beside this file.

The circuit (U1 is one dual rail-to-rail CMOS op-amp, such as an MCP6002, on the ESP32's 3.3 V):

                R1 22k            R2 22k
    CT ──┬──┬───/\\/\\/──┬──────────/\\/\\/─────┐
         │  │          │   ┌──────────┐     │
        Rb  TVS        └───┤−   U1a   ├─────┴── A
        47R │          ┌───┤+         │
         │  │          │   └──────────┘
    GND ─┴──┴──────────┘

                     3V3
                      │
                     R4 3M3
           R3 100k    │    ┌──────────┐          R7 1k
    A ─────/\\/\\/──────┼────┤+   U1b   ├──┬───────/\\/\\/──┬── ADC pin
                      │  ┌─┤−         │  │              │
                  C1 4µ7 │ └──────────┘  │           C2 100n
                      │  ├────/\\/\\/──────┘              │
                     GND │   R5 20k                    GND
                        R6 10k
                         │
                        GND

U1a is an inverting amplifier with no negative supply, so it passes one half of each cycle and sits at
0 V for the other: a rectifier with no diode drop. R3 and C1 average it, R4 lifts the resting level
clear of the bottom of the ESP32's ADC range, and U1b amplifies the result.

The burden is as large as the transformer comfortably drives and U1a's gain is only 1, because U1a's
offset voltage is what limits accuracy at small currents (section 2 of the output): the bigger the
signal at its input, the less the offset matters.

What is modelled: the op-amps' gain-bandwidth, slew rate, offset voltage, output limits and input
protection diodes; the current transformer's winding resistance and magnetising inductance; the TVS.
What is not: the transformer's core saturating (so large overloads look worse here than they are),
noise, and the ESP32 ADC's own errors. There is no SPICE engine on PyPI, so the circuit's equations
are written out and stepped through time below (see Circuit).
"""

import math
import sys
from pathlib import Path

import numpy as np

MAINS_HZ = 50
HERE = Path(__file__).parent

# What the firmware assumes (app/config.py).
FIRMWARE_MV_PER_AMP = 61.6
FIRMWARE_ZERO_MV = 290
FIRMWARE_ZERO_TOLERANCE_MV = 150
FIRMWARE_ZERO_SETTLE_S = 5
ADC_MIN_MV, ADC_MAX_MV = 150, 2450  # roughly what the ESP32 can read at 11 dB attenuation


class Parts:
    """Part values. The op-amp figures are the MCP6002's; change them to try another."""

    def __init__(self, **changes):
        self.vdd = 3.3
        self.ct_turns = 1000
        self.ct_winding_ohms = 50  # a guess at a typical 1000-turn transformer
        self.ct_henries = 5.0  # likewise: its magnetising inductance, seen from the secondary
        self.rb = 47.0
        self.r1 = 22e3
        self.r2 = 22e3
        self.r3 = 100e3
        self.r4 = 3.3e6
        self.r5 = 20e3
        self.r6 = 10e3
        self.r7 = 1e3
        self.c1 = 4.7e-6
        self.c2 = 100e-9
        self.gbw_hz = 1e6
        self.slew_v_per_s = 0.6e6
        self.vos_a = 0.0  # U1a's input offset voltage (the data sheet's limit is +/-4.5 mV)
        self.vos_b = 0.0
        # How close the outputs get to ground. The data sheet's 25 mV is with 10k of load; here U1a sinks
        # a few microamps through R2, and a CMOS output is then within a millivolt or so.
        self.out_low = 0.001
        self.out_high_drop = 0.025
        self.tvs_volts = 6.4  # SMAJ5.0CA: where it starts to conduct
        self.tvs_ohms = 1.0
        for name, value in changes.items():
            if not hasattr(self, name):
                raise AttributeError(name)
            setattr(self, name, value)

    def predicted_mv_per_amp(self):
        peak = math.sqrt(2) * self.rb / self.ct_turns  # burden volts, peak, for 1 A rms
        return 1000 * peak * (self.r2 / self.r1) / math.pi * self.r4 / (self.r3 + self.r4) * (1 + self.r5 / self.r6)

    def predicted_zero_mv(self):
        return 1000 * self.vdd * self.r3 / (self.r3 + self.r4) * (1 + self.r5 / self.r6)


DIODE_IS = 1e-14  # the op-amp's input protection diodes
DIODE_VT = 0.026


def diode(volts):
    """Current through a protection diode with `volts` across it, and its slope."""
    x = min(volts / DIODE_VT, 40)  # above this (about 1 V) carry on in a straight line
    e = math.exp(x)
    current = DIODE_IS * (e - 1)
    slope = DIODE_IS * e / DIODE_VT
    if volts / DIODE_VT > 40:
        current += slope * (volts - 40 * DIODE_VT)
    return current, slope


class Circuit:
    """
    The circuit, stepped through time. `secondary_amps(t)` is the current the transformer's secondary
    would deliver into a short circuit.

    General-purpose equation solvers trip over the op-amps hitting their supply rails, so this steps
    the circuit itself, in steps much shorter than a mains cycle. Each op-amp is an integrator (its
    gain-bandwidth) with a slew limit and output limits, advanced with the backward Euler method, which
    stays stable however fast the op-amp is next to the step. The capacitors and the transformer change
    slowly and are advanced directly.
    """

    def __init__(self, parts, secondary_amps):
        self.p = parts
        self.secondary_amps = secondary_amps
        self._guess = (0.0, 0.0)

    def input_nodes(self, source_amps, out_a=None, out_before=0.0, hk=0.0):
        """
        The burden voltage and U1a's inverting input, given the current arriving.

        With out_a given, U1a's output is held there. Without, U1a is free to move from out_before, and
        its output after the step is out_before + hk x (its offset - the inverting input).
        """
        p = self.p
        g_b = 1 / p.rb if p.rb else 0.0
        free = out_a is None
        vb, vn = self._guess
        for _ in range(400):
            # Current out of the burden node through the TVS.
            over = abs(vb) - p.tvs_volts
            i_tvs = math.copysign(over / p.tvs_ohms, vb) if over > 0 else 0.0
            g_tvs = 1 / p.tvs_ohms if over > 0 else 0.0
            # Current out of the inverting input through the diodes to each supply rail.
            i_up, g_up = diode(vn - p.vdd)
            i_down, g_down = diode(-vn)
            out = out_before + hk * (p.vos_a - vn) if free else out_a
            f1 = source_amps - vb * g_b - (vb - vn) / p.r1 - i_tvs
            f2 = (vb - vn) / p.r1 + (out - vn) / p.r2 - i_up + i_down
            a = -g_b - 1 / p.r1 - g_tvs
            b = 1 / p.r1
            c = 1 / p.r1
            d = -1 / p.r1 - (1 + (hk if free else 0.0)) / p.r2 - g_up - g_down
            det = a * d - b * c
            step_b = (-f1 * d + f2 * b) / det
            step_n = (-a * f2 + c * f1) / det
            biggest = max(abs(step_b), abs(step_n))
            if biggest > 0.1:  # the diodes are exponential: don't leap
                step_b *= 0.1 / biggest
                step_n *= 0.1 / biggest
            vb += step_b
            vn += step_n
            if biggest < 1e-10:
                break
        else:
            raise RuntimeError("the input network did not settle")
        self._guess = (vb, vn)
        return vb, vn

    def resting_state(self):
        p = self.p
        out_a = max(p.out_low, p.vos_a * (1 + p.r2 / p.r1))
        v_c1 = (out_a * p.r4 + p.vdd * p.r3) / (p.r3 + p.r4)
        out_b = (v_c1 + p.vos_b) * (1 + p.r5 / p.r6)
        return [0.0, out_a, v_c1, out_b, out_b]

    def run(self, seconds, step=10e-6, keep_every=10):
        p = self.p
        i_mag, out_a, v_c1, out_b, v_c2 = self.resting_state()
        hk = step * 2 * math.pi * p.gbw_hz
        max_move = step * p.slew_v_per_s
        low, high = p.out_low, p.vdd - p.out_high_drop
        keep_c1 = math.exp(-step * (1 / p.r3 + 1 / p.r4) / p.c1)
        keep_c2 = math.exp(-step / (p.r7 * p.c2))
        beta = p.r6 / (p.r5 + p.r6)
        count = int(round(seconds / step))
        kept = []
        for n in range(1, count + 1):
            t = n * step
            source = self.secondary_amps(t) - i_mag

            # U1a and the network around its input.
            vb, vn = self.input_nodes(source, None, out_a, hk)
            new_a = out_a + hk * (p.vos_a - vn)
            limited = min(max(new_a, out_a - max_move, low), out_a + max_move, high)
            if limited != new_a:  # slewing, or against a supply rail
                new_a = limited
                vb, vn = self.input_nodes(source, new_a)
            out_a = new_a

            i_mag += step * (vb + p.ct_winding_ohms * source) / p.ct_henries
            target = (out_a * p.r4 + p.vdd * p.r3) / (p.r3 + p.r4)
            v_c1 = target + (v_c1 - target) * keep_c1

            # U1b, a non-inverting amplifier.
            new_b = (out_b + hk * (v_c1 + p.vos_b)) / (1 + hk * beta)
            out_b = min(max(new_b, out_b - max_move, low), out_b + max_move, high)
            v_c2 = out_b + (v_c2 - out_b) * keep_c2

            if n % keep_every == 0:
                diode_amps = (vb - vn) / p.r1 + (out_a - vn) / p.r2
                kept.append((t, out_a, v_c1, out_b, v_c2, vb, vn, diode_amps))
        return Run(np.array(kept).T)


class Run:
    def __init__(self, columns):
        self.t, self.out_a, self.v_c1, self.out_b, self.adc, self.vb, self.vn, self.diode_amps = columns

    def last_cycles(self, cycles=5):
        return self.t > self.t[-1] - cycles / MAINS_HZ + 1e-9


def sine(amps_rms, turns=1000):
    peak = amps_rms * math.sqrt(2) / turns
    return lambda t: peak * math.sin(2 * math.pi * MAINS_HZ * t)


def settled_mv(parts, amps_rms, step=5e-6):
    """
    What the ADC pin settles to with a steady sine-wave current, in millivolts.

    Waiting for the 0.5 s filter is slow to simulate, so this simulates three cycles and works the rest
    out from U1a's output: C1 settles to its average, and U1b just multiplies. (The slower, complete
    simulations further down check that this agrees.)
    """
    if amps_rms == 0:
        # With no signal U1a amplifies only its own offset, or sits at its lower limit.
        mean_a = max(parts.out_low, parts.vos_a * (1 + parts.r2 / parts.r1))
    else:
        quick = Parts(**{**vars(parts), "ct_henries": 1e9})  # no start-up surge in the transformer
        run = Circuit(quick, sine(amps_rms, parts.ct_turns)).run(3 / MAINS_HZ, step=step, keep_every=1)
        mean_a = run.out_a[run.last_cycles(1)].mean()
    v_c1 = (mean_a * parts.r4 + parts.vdd * parts.r3) / (parts.r3 + parts.r4)
    out_b = (v_c1 + parts.vos_b) * (1 + parts.r5 / parts.r6)
    return 1000 * max(parts.out_low, min(parts.vdd - parts.out_high_drop, out_b))


def heading(text):
    print("\n" + text)
    print("-" * len(text))


def transfer_curve(plot):
    heading("1. Steady readings, nominal parts")
    nominal = Parts()
    print("Predicted by hand: zero %.0f mV, %.1f mV per amp" % (nominal.predicted_zero_mv(), nominal.predicted_mv_per_amp()))
    zero = settled_mv(nominal, 0)
    print("  amps    pin mV   firmware reads   error")
    rows = []
    for amps in (0, 0.25, 0.5, 1, 2.5, 5, 10, 13, 16, 20, 30, 35, 40, 50):
        mv = settled_mv(nominal, amps)
        reads = max(0, mv - zero) / FIRMWARE_MV_PER_AMP
        note = "   (beyond the ADC's range)" if mv > ADC_MAX_MV else ""
        print("  %5.2f   %6.0f   %8.2f A     %+5.1f%%%s" % (amps, mv, reads, 100 * (reads / amps - 1) if amps else 0, note))
        rows.append((amps, mv))
    plot.transfer(rows, nominal)
    finer = settled_mv(nominal, 10, step=1e-6)
    print("Check on the method: with a step five times shorter, 10 A gives %.2f mV against %.2f mV." % (finer, settled_mv(nominal, 10)))


def offset_corners():
    heading("2. Small currents against U1a's offset voltage (after the firmware's auto-zero)")
    print("Data sheet limits: TLV9002 and TLV9062 +/-1.6 mV, MCP6002 +/-4.5 mV, LMV358 +/-7 mV.")
    print("  U1a offset    zero mV    reads at: 0.25 A   0.5 A    1 A    2.5 A    5 A")
    for vos_mv in (-7, -4.5, -1.6, 0, 1.6, 4.5, 7):
        parts = Parts(vos_a=vos_mv / 1000)
        zero = settled_mv(parts, 0)
        reads = [max(0, settled_mv(parts, amps) - zero) / FIRMWARE_MV_PER_AMP for amps in (0.25, 0.5, 1, 2.5, 5)]
        print("  %+5.1f mV     %6.0f              %5.2f   %5.2f   %5.2f   %5.2f   %5.2f" % (vos_mv, zero, *reads))
    print("The offset takes a roughly fixed amount off every reading, whichever its sign.")


def tolerances(units=200):
    heading("3. %d boards with parts spread over their tolerances" % units)
    print("Resistors 1%, 3.3 V rail 3%, transformer ratio 1%, output low 0.5-5 mV, op-amp offsets as shown.")
    nominal = Parts()
    for name, vos_limit in (("TLV9002", 1.6e-3), ("MCP6002", 4.5e-3)):
        rng = np.random.default_rng(1)
        zeros, at_1, at_threshold, at_13 = [], [], [], []
        for _ in range(units):
            changes = {name: getattr(nominal, name) * rng.uniform(0.99, 1.01) for name in ("rb", "r1", "r2", "r3", "r4", "r5", "r6", "ct_turns")}  # fmt: skip
            changes["vdd"] = 3.3 * rng.uniform(0.97, 1.03)
            changes["vos_a"] = rng.uniform(-vos_limit, vos_limit)
            changes["vos_b"] = rng.uniform(-vos_limit, vos_limit)
            changes["out_low"] = rng.uniform(0.0005, 0.005)
            parts = Parts(**changes)
            zero = settled_mv(parts, 0)
            zeros.append(zero)
            at_1.append((settled_mv(parts, 1) - zero) / FIRMWARE_MV_PER_AMP)
            at_threshold.append((settled_mv(parts, 2.5) - zero) / FIRMWARE_MV_PER_AMP)
            at_13.append((settled_mv(parts, 13) - zero) / FIRMWARE_MV_PER_AMP)
        print("%s (offset up to +/-%.1f mV):" % (name, vos_limit * 1000))
        print("  zero reading:        %.0f to %.0f mV (the firmware accepts %d to %d; the ADC reads down to about %d)"
              % (min(zeros), max(zeros), FIRMWARE_ZERO_MV - FIRMWARE_ZERO_TOLERANCE_MV,
                 FIRMWARE_ZERO_MV + FIRMWARE_ZERO_TOLERANCE_MV, ADC_MIN_MV))  # fmt: skip
        print("  a true 1 A reads:    %.2f to %.2f A" % (min(at_1), max(at_1)))
        print("  a true 2.5 A reads:  %.2f to %.2f A" % (min(at_threshold), max(at_threshold)))
        print("  a true 13 A reads:   %.2f to %.2f A" % (min(at_13), max(at_13)))


def switching(plot):
    heading("4. A 10 A machine switched on at 1 s and off at 5 s (complete simulation)")
    current = sine(10)
    circuit = Circuit(Parts(), lambda t: current(t) if 1 <= t < 5 else 0.0)
    run = circuit.run(10.0, step=20e-6, keep_every=5)
    mv = run.adc * 1000
    zero = mv[(run.t > 0.9) & (run.t < 1)].mean()
    on = (run.t > 4.8) & (run.t < 5)
    level = mv[on].mean()
    print("Zero %.0f mV (quick method: %.0f). Running %.0f mV (quick method: %.0f)." % (zero, settled_mv(Parts(), 0), level, settled_mv(Parts(), 10)))
    print("Ripple while running: %.0f mV peak to peak, so a reading wanders by +/-%.2f A." % (np.ptp(mv[on]), np.ptp(mv[on]) / FIRMWARE_MV_PER_AMP / 2))
    rising = run.t[(run.t > 1) & (mv > zero + 0.9 * (level - zero))][0] - 1
    threshold_mv = zero + 2.5 * FIRMWARE_MV_PER_AMP
    crossed = run.t[(run.t > 1) & (mv > threshold_mv)][0] - 1
    fell = run.t[(run.t > 5) & (mv < threshold_mv)][0] - 5
    print("Reaches 90%% in %.2f s. Crosses a 2.5 A threshold %.2f s after switch-on and %.2f s after switch-off." % (rising, crossed, fell))
    settle = mv[run.t >= 5 + FIRMWARE_ZERO_SETTLE_S - 1e-9][0] - zero
    print("The firmware measures zero from %d s after the relay opens; by then the pin is %.2f mV above zero." % (FIRMWARE_ZERO_SETTLE_S, settle))
    plot.switching(run)


def inrush(plot):
    heading("5. Starting surge: 150 A for 0.2 s, then 15 A (no core saturation modelled, so pessimistic)")
    surge, steady = sine(150), sine(15)
    circuit = Circuit(Parts(), lambda t: (surge(t) if t < 0.3 else steady(t)) if t >= 0.1 else 0.0)
    run = circuit.run(3.0)
    vb, vn, diode_amps = run.vb, run.vn, run.diode_amps
    print("Burden voltage peaks at %.1f V; the TVS (%.1f V) %s." % (np.abs(vb).max(), Parts().tvs_volts, "conducts" if np.abs(vb).max() > Parts().tvs_volts else "stays off"))
    print("U1a's inverting input swings from %.2f V to %.2f V." % (vn.min(), vn.max()))
    print("Current in its protection diodes: %.2f mA at most (op-amps allow 2 mA or more)." % (np.abs(diode_amps).max() * 1000))
    print("ADC pin peaks at %.2f V (it can never pass the op-amp's %.1f V supply)." % (run.adc.max(), Parts().vdd))
    end = run.adc[run.last_cycles(5)].mean() * 1000
    print("Afterwards it settles to %.0f mV; a steady 15 A gives %.0f mV." % (end, settled_mv(Parts(), 15)))
    plot.inrush(run, vb, vn)


def odd_waveforms():
    heading("6. Currents that aren't sine waves")
    print("The circuit measures the average of the rectified current, and the firmware scales that as if")
    print("it were a sine wave. For other shapes it reads low; that is fine for a threshold.")
    w = 2 * math.pi * MAINS_HZ
    peak = 10 * math.sqrt(2) / 1000

    def chopped(t):  # a dimmer or speed controller at half power: each half-cycle starts a quarter-cycle late
        phase = (w * t) % math.pi
        return peak * math.sin(w * t) if phase > math.pi / 2 else 0.0

    def half_wave(sign):  # a load with a diode in series: current flows in one direction only
        return lambda t: max(0.0, sign * peak * math.sin(w * t)) * sign

    zero = settled_mv(Parts(), 0)
    print("  waveform                              true rms    reads")
    for name, wave, rms in (
        ("10 A sine", sine(10), 10.0),
        ("the same, chopped at 90 degrees", chopped, 10 / math.sqrt(2)),
        ("half-wave, positive half only", half_wave(1), 10 / math.sqrt(2)),
        ("half-wave, negative half only", half_wave(-1), 10 / math.sqrt(2)),
    ):
        run = Circuit(Parts(), wave).run(4.0, step=20e-6, keep_every=5)
        mv = run.adc[run.last_cycles(5)].mean() * 1000
        print("  %-36s  %5.2f A   %5.2f A" % (name, rms, (mv - zero) / FIRMWARE_MV_PER_AMP))
    print("The two half-wave readings should match: the transformer can't pass DC, so both halves of its")
    print("output have the same area, whichever way round the load is.")


def burden_missing():
    heading("7. Fault: the burden resistor open-circuit, 15 A flowing")
    circuit = Circuit(Parts(rb=0), sine(15))  # rb=0 here means "not fitted"
    run = circuit.run(0.2)
    vb, vn, diode_amps = run.vb, run.vn, run.diode_amps
    print("The TVS holds the transformer to %.1f V peak." % np.abs(vb).max())
    print("Current in U1a's protection diodes: %.2f mA at most. ADC pin at most %.2f V." % (np.abs(diode_amps).max() * 1000, run.adc.max()))


class Plots:
    def __init__(self):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        self.plt = plt

    def save(self, figure, name):
        figure.tight_layout()
        figure.savefig(HERE / name, dpi=110)
        self.plt.close(figure)
        print("(graph: current_sensor/%s)" % name)

    def transfer(self, rows, parts):
        figure, axis = self.plt.subplots(figsize=(7, 4))
        amps = [row[0] for row in rows]
        axis.plot(amps, [row[1] for row in rows], "o-", label="simulated")
        axis.plot(amps, [parts.predicted_zero_mv() + parts.predicted_mv_per_amp() * a for a in amps], "--", label="predicted by hand")
        axis.axhspan(ADC_MAX_MV, 3300, color="0.85", label="beyond the ADC's range")
        axis.axhspan(0, ADC_MIN_MV, color="0.85")
        axis.set_xlabel("machine current (A rms, sine)")
        axis.set_ylabel("ADC pin (mV)")
        axis.set_ylim(0, 3300)
        axis.legend()
        axis.grid(alpha=0.3)
        self.save(figure, "transfer.png")

    def switching(self, run):
        figure, axes = self.plt.subplots(2, 1, figsize=(8, 6))
        axes[0].plot(run.t, run.adc * 1000)
        axes[0].set_ylabel("ADC pin (mV)")
        axes[0].set_xlabel("seconds (10 A on at 1 s, off at 5 s)")
        axes[0].grid(alpha=0.3)
        detail = (run.t > 4.0) & (run.t < 4.06)
        axes[1].plot(run.t[detail], run.out_a[detail], label="U1a output (point A)")
        axes[1].plot(run.t[detail], run.v_c1[detail], label="C1")
        axes[1].plot(run.t[detail], run.adc[detail], label="ADC pin")
        axes[1].set_ylabel("volts")
        axes[1].set_xlabel("seconds (three cycles while running)")
        axes[1].legend()
        axes[1].grid(alpha=0.3)
        self.save(figure, "switching.png")

    def inrush(self, run, vb, vn):
        figure, axes = self.plt.subplots(2, 1, figsize=(8, 6))
        detail = run.t < 0.4
        axes[0].plot(run.t[detail], vb[detail], label="burden")
        axes[0].plot(run.t[detail], vn[detail], label="U1a inverting input")
        axes[0].plot(run.t[detail], run.out_a[detail], label="U1a output")
        axes[0].set_ylabel("volts")
        axes[0].set_xlabel("seconds (150 A surge from 0.1 s to 0.3 s)")
        axes[0].legend()
        axes[0].grid(alpha=0.3)
        axes[1].plot(run.t, run.adc * 1000)
        axes[1].set_ylabel("ADC pin (mV)")
        axes[1].set_xlabel("seconds")
        axes[1].grid(alpha=0.3)
        self.save(figure, "inrush.png")


def main():
    plot = Plots()
    transfer_curve(plot)
    offset_corners()
    tolerances(units=int(sys.argv[1]) if len(sys.argv) > 1 else 200)
    switching(plot)
    inrush(plot)
    odd_waveforms()
    burden_missing()


if __name__ == "__main__":
    main()
