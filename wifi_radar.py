#!/usr/bin/env python3
"""
wifi_radar.py — Detect movement/presence by monitoring Wi-Fi signal disruption.

Requirements:
    pip install scapy numpy scipy matplotlib

Usage:
    sudo python3 wifi_radar.py --interface wlan0
    sudo python3 wifi_radar.py --interface wlan0 --channel 6 --sensitivity 2.0
"""

import argparse
import collections
import subprocess
import sys
import threading
import time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from scipy.ndimage import uniform_filter1d

try:
    from scapy.all import sniff, RadioTap, Dot11
except ImportError:
    sys.exit("Install scapy first: pip install scapy")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUFFER_SIZE = 500          # Number of RSSI samples to keep in memory
BASELINE_SAMPLES = 100     # Samples used to compute baseline (calibration period)
MOVEMENT_THRESHOLD = 2.0   # Z-score threshold to flag movement (lower = more sensitive)
SMOOTHING_WINDOW = 5       # Rolling average window for noise reduction
ALERT_COOLDOWN = 2.0       # Seconds between alerts to avoid spam


# ---------------------------------------------------------------------------
# Monitor mode helpers
# ---------------------------------------------------------------------------

def set_monitor_mode(interface: str, channel: int | None = None) -> None:
    """Put the Wi-Fi interface into monitor mode."""
    print(f"[*] Setting {interface} to monitor mode...")
    try:
        subprocess.run(["ip", "link", "set", interface, "down"], check=True)
        subprocess.run(["iw", interface, "set", "monitor", "none"], check=True)
        subprocess.run(["ip", "link", "set", interface, "up"], check=True)
        if channel:
            subprocess.run(["iw", interface, "set", "channel", str(channel)], check=True)
            print(f"[*] Locked to channel {channel}")
        print(f"[+] Monitor mode active on {interface}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"[-] Failed to set monitor mode: {e}\n    Run as root (sudo).")


def restore_managed_mode(interface: str) -> None:
    """Restore the interface to managed (normal) mode on exit."""
    print(f"\n[*] Restoring {interface} to managed mode...")
    subprocess.run(["ip", "link", "set", interface, "down"], capture_output=True)
    subprocess.run(["iw", interface, "set", "type", "managed"], capture_output=True)
    subprocess.run(["ip", "link", "set", interface, "up"], capture_output=True)
    print(f"[+] Done.")


# ---------------------------------------------------------------------------
# Radar core
# ---------------------------------------------------------------------------

class WifiRadar:
    def __init__(self, interface: str, sensitivity: float = MOVEMENT_THRESHOLD):
        self.interface = interface
        self.sensitivity = sensitivity

        self.rssi_buffer = collections.deque(maxlen=BUFFER_SIZE)
        self.timestamps = collections.deque(maxlen=BUFFER_SIZE)
        self.movement_events = []  # list of timestamps where movement was detected

        self._lock = threading.Lock()
        self._last_alert = 0.0
        self._packet_count = 0
        self._calibrated = False

        print(f"[*] Sensitivity: {sensitivity} (lower = more sensitive)")
        print(f"[*] Calibrating baseline over first {BASELINE_SAMPLES} samples...\n")

    # ------------------------------------------------------------------
    # Packet processing
    # ------------------------------------------------------------------

    def _extract_rssi(self, packet) -> int | None:
        """Extract RSSI (dBm) from RadioTap header."""
        if packet.haslayer(RadioTap):
            try:
                return int(packet[RadioTap].dBm_AntSignal)
            except (AttributeError, TypeError):
                pass
        return None

    def packet_callback(self, packet) -> None:
        """Called for every captured packet."""
        rssi = self._extract_rssi(packet)
        if rssi is None or rssi == 0:
            return

        now = time.time()
        with self._lock:
            self.rssi_buffer.append(rssi)
            self.timestamps.append(now)
            self._packet_count += 1

        self._detect_movement()

    # ------------------------------------------------------------------
    # Movement detection
    # ------------------------------------------------------------------

    def _detect_movement(self) -> None:
        with self._lock:
            samples = list(self.rssi_buffer)
            n = len(samples)

        if n < BASELINE_SAMPLES:
            return  # still calibrating

        if not self._calibrated:
            self._calibrated = True
            print("[+] Calibration complete. Monitoring for movement...\n")

        # Smooth the signal
        smoothed = uniform_filter1d(samples, size=SMOOTHING_WINDOW)

        # Baseline: mean and std of all but the last few samples
        baseline = smoothed[:-SMOOTHING_WINDOW]
        mean = np.mean(baseline)
        std = np.std(baseline)

        if std < 0.5:
            std = 0.5  # avoid division by near-zero on very stable signals

        # Z-score of the most recent value
        current = smoothed[-1]
        z_score = abs(current - mean) / std

        now = time.time()
        if z_score > self.sensitivity and (now - self._last_alert) > ALERT_COOLDOWN:
            self._last_alert = now
            with self._lock:
                self.movement_events.append(now)
            print(f"[!] MOVEMENT DETECTED  |  RSSI: {current:.1f} dBm  |  z-score: {z_score:.2f}")

    # ------------------------------------------------------------------
    # Sniffing (runs in background thread)
    # ------------------------------------------------------------------

    def start_sniffing(self) -> None:
        """Start packet capture in a daemon thread."""
        def _sniff():
            sniff(
                iface=self.interface,
                prn=self.packet_callback,
                store=False,
                filter="type mgt or type data",  # management + data frames only
            )

        t = threading.Thread(target=_sniff, daemon=True)
        t.start()
        print(f"[+] Sniffing on {self.interface} — press Ctrl+C to stop\n")

    # ------------------------------------------------------------------
    # Live visualisation
    # ------------------------------------------------------------------

    def run_plot(self) -> None:
        """Open a real-time matplotlib window showing RSSI over time."""
        fig, (ax_rssi, ax_event) = plt.subplots(2, 1, figsize=(12, 6), gridspec_kw={"height_ratios": [3, 1]})
        fig.suptitle("Wi-Fi Radar — Signal Disruption Monitor", fontsize=13)

        line_raw, = ax_rssi.plot([], [], color="steelblue", alpha=0.5, linewidth=0.8, label="Raw RSSI")
        line_smooth, = ax_rssi.plot([], [], color="royalblue", linewidth=1.5, label="Smoothed")
        line_mean, = ax_rssi.plot([], [], color="gray", linewidth=1, linestyle="--", label="Baseline mean")

        ax_rssi.set_ylabel("RSSI (dBm)")
        ax_rssi.set_ylim(-100, -20)
        ax_rssi.legend(loc="upper right", fontsize=8)
        ax_rssi.grid(True, alpha=0.3)

        ax_event.set_ylabel("Movement")
        ax_event.set_ylim(-0.5, 1.5)
        ax_event.set_yticks([0, 1])
        ax_event.set_yticklabels(["Clear", "Detected"])
        ax_event.grid(True, alpha=0.3)

        status_text = ax_rssi.text(0.01, 0.95, "", transform=ax_rssi.transAxes,
                                    fontsize=9, verticalalignment="top", color="white",
                                    bbox=dict(boxstyle="round", facecolor="steelblue", alpha=0.7))

        def update(_frame):
            with self._lock:
                samples = list(self.rssi_buffer)
                events = list(self.movement_events)
                count = self._packet_count

            if len(samples) < 2:
                return line_raw, line_smooth, line_mean

            x = list(range(len(samples)))
            smoothed = uniform_filter1d(samples, size=SMOOTHING_WINDOW).tolist()
            mean_val = np.mean(samples[: max(1, len(samples) - SMOOTHING_WINDOW)])

            line_raw.set_data(x, samples)
            line_smooth.set_data(x, smoothed)
            line_mean.set_data([0, len(samples) - 1], [mean_val, mean_val])

            ax_rssi.set_xlim(0, max(BUFFER_SIZE, len(samples)))

            # Movement event markers
            ax_event.cla()
            ax_event.set_ylabel("Movement")
            ax_event.set_ylim(-0.5, 1.5)
            ax_event.set_yticks([0, 1])
            ax_event.set_yticklabels(["Clear", "Detected"])
            ax_event.set_xlim(0, max(BUFFER_SIZE, len(samples)))
            ax_event.grid(True, alpha=0.3)
            ax_event.axhline(0, color="green", alpha=0.4)

            now = time.time()
            for evt in events[-20:]:
                # Rough mapping: find approximate x position
                with self._lock:
                    ts_list = list(self.timestamps)
                if ts_list:
                    idx = min(range(len(ts_list)), key=lambda i: abs(ts_list[i] - evt))
                    ax_event.axvline(idx, color="red", alpha=0.7, linewidth=1.5)

            # Status label
            calibrated = "Calibrated" if self._calibrated else f"Calibrating ({len(samples)}/{BASELINE_SAMPLES})"
            status_text.set_text(f"Packets: {count}  |  {calibrated}  |  Events: {len(events)}")

            return line_raw, line_smooth, line_mean

        ani = animation.FuncAnimation(fig, update, interval=200, blit=False, cache_frame_data=False)
        plt.tight_layout()
        try:
            plt.show()
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Wi-Fi Radar — detect movement via signal disruption")
    parser.add_argument("--interface", "-i", required=True, help="Wi-Fi interface (e.g. wlan0)")
    parser.add_argument("--channel", "-c", type=int, default=None, help="Lock to a specific channel (1-13)")
    parser.add_argument("--sensitivity", "-s", type=float, default=MOVEMENT_THRESHOLD,
                        help=f"Detection sensitivity / z-score threshold (default: {MOVEMENT_THRESHOLD})")
    parser.add_argument("--no-monitor-setup", action="store_true",
                        help="Skip monitor mode setup (if already configured)")
    args = parser.parse_args()

    if not args.no_monitor_setup:
        set_monitor_mode(args.interface, args.channel)

    radar = WifiRadar(args.interface, sensitivity=args.sensitivity)

    try:
        radar.start_sniffing()
        radar.run_plot()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_monitor_setup:
            restore_managed_mode(args.interface)


if __name__ == "__main__":
    main()
