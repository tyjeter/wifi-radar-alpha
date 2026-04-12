#!/usr/bin/env python3
"""
wifi_radar.py — Detect movement/presence by monitoring Wi-Fi signal disruption.

Features:
  - Z-score movement detection with false-positive filtering
  - Direction (approaching / moving away) and speed estimation
  - FFT-based breathing / micro-motion detection
  - Fall detection (spike + subsequent quiet)
  - Dual-adapter active mode (TX probe injection + RX monitoring)
  - Home Assistant webhook integration
  - Occupancy tracking (daily hours logged to SQLite)
  - SQLite history persistence via db.py

Usage:
    sudo python3 wifi_radar.py --interface wlan0
    sudo python3 wifi_radar.py --interface wlan0 --tx-interface wlan1 --channel 6
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
    from scapy.all import sniff, sendp, RadioTap, Dot11, Dot11ProbeReq, Dot11Elt
except ImportError:
    sys.exit("Install scapy first: pip install scapy")

try:
    import db
    import config as cfg_mod
    _HAS_MODULES = True
except ImportError:
    _HAS_MODULES = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUFFER_SIZE        = 2000
BASELINE_SAMPLES   = 100
MOVEMENT_THRESHOLD = 2.0
SMOOTHING_WINDOW   = 5
ALERT_COOLDOWN     = 2.0
DIRECTION_WINDOW   = 25         # samples for direction regression
SPEED_SLOW_DBM_S   = 0.05       # |dBm/s| slow threshold
SPEED_FAST_DBM_S   = 0.40       # |dBm/s| fast threshold
FALL_MULTIPLIER    = 3.0        # z-score × multiplier = fall threshold
FALL_SILENCE_S     = 3.0        # seconds of quiet after spike = fall
BREATHING_MIN_HZ   = 0.1
BREATHING_MAX_HZ   = 0.6
HEARTBEAT_MIN_HZ   = 0.8
HEARTBEAT_MAX_HZ   = 2.0
BREATHING_WINDOW_S = 60         # seconds of history for FFT
FFT_SAMPLE_RATE    = 10.0       # Hz for resampled signal
INTERFERENCE_SPIKE_RATE = 18    # spikes/s = interference
OCCUPANCY_CHECK_S  = 10.0       # seconds between occupancy updates
DB_WRITE_INTERVAL  = 1.0        # seconds between DB writes


# ---------------------------------------------------------------------------
# Monitor mode helpers
# ---------------------------------------------------------------------------

def set_monitor_mode(interface: str, channel: int | None = None) -> None:
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
    print(f"\n[*] Restoring {interface} to managed mode...")
    subprocess.run(["ip", "link", "set", interface, "down"], capture_output=True)
    subprocess.run(["iw", interface, "set", "type", "managed"], capture_output=True)
    subprocess.run(["ip", "link", "set", interface, "up"], capture_output=True)
    print("[+] Done.")


def restart_interface(interface: str, channel: int | None = None) -> None:
    """Take interface down and back up — recover from driver hangs."""
    subprocess.run(["ip", "link", "set", interface, "down"], capture_output=True)
    time.sleep(0.5)
    subprocess.run(["ip", "link", "set", interface, "up"], capture_output=True)
    if channel:
        subprocess.run(["iw", interface, "set", "channel", str(channel)], capture_output=True)


# ---------------------------------------------------------------------------
# Radar core
# ---------------------------------------------------------------------------

class WifiRadar:
    def __init__(
        self,
        interface: str,
        sensitivity: float = MOVEMENT_THRESHOLD,
        tx_interface: str | None = None,
        channel: int | None = None,
        ha_webhook_url: str | None = None,
        ha_token: str | None = None,
        fall_multiplier: float = FALL_MULTIPLIER,
        breathing_detection: bool = True,
        false_positive_filter: bool = True,
        occupancy_tracking: bool = True,
        use_db: bool = True,
    ):
        self.interface           = interface
        self.sensitivity         = sensitivity
        self.tx_interface        = tx_interface
        self.channel             = channel
        self.ha_webhook_url      = ha_webhook_url
        self.ha_token            = ha_token
        self.fall_multiplier     = fall_multiplier
        self.breathing_detection = breathing_detection
        self.fp_filter           = false_positive_filter
        self.occupancy_tracking  = occupancy_tracking

        # Buffers
        self.rssi_buffer    = collections.deque(maxlen=BUFFER_SIZE)
        self.timestamps     = collections.deque(maxlen=BUFFER_SIZE)
        self.movement_events: list[dict] = []   # {ts, z_score, rssi}
        self.fall_events:    list[dict] = []    # {ts, z_score}
        self.zone_alerts:    list[dict] = []    # {zone, ts, rssi}

        # Mutable state (guarded by _lock)
        self._lock               = threading.Lock()
        self._last_alert         = 0.0
        self._last_fall_alert    = 0.0
        self._packet_count       = 0
        self._calibrated         = False
        self._direction          = "unknown"
        self._speed              = "unknown"
        self._breathing_detected = False
        self._breathing_freq     = None
        self._heartbeat_detected = False
        self._heartbeat_freq     = None
        self._alert_zones: list[dict] = []

        # Occupancy
        self._occupancy_start   = None
        self._last_occ_check    = 0.0

        # DB
        self._db_enabled     = False
        self._last_db_write  = 0.0
        self._breath_check_ts = 0.0

        # False-positive: track recent z-threshold crossings
        self._spike_times = collections.deque(maxlen=100)

        if _HAS_MODULES and use_db:
            db.init_db()
            self._db_enabled = True

        print(f"[*] Sensitivity: {sensitivity} (lower = more sensitive)")
        print(f"[*] Calibrating over first {BASELINE_SAMPLES} samples...\n")

    # ------------------------------------------------------------------
    # Public configuration
    # ------------------------------------------------------------------

    def set_alert_zones(self, zones: list) -> None:
        with self._lock:
            self._alert_zones = zones

    def set_sensitivity(self, s: float) -> None:
        self.sensitivity = s

    def get_packet_count(self) -> int:
        return self._packet_count

    # ------------------------------------------------------------------
    # Packet processing
    # ------------------------------------------------------------------

    def _extract_rssi(self, packet) -> int | None:
        if packet.haslayer(RadioTap):
            try:
                return int(packet[RadioTap].dBm_AntSignal)
            except (AttributeError, TypeError):
                pass
        return None

    def packet_callback(self, packet) -> None:
        rssi = self._extract_rssi(packet)
        if rssi is None or rssi == 0:
            return

        now = time.time()
        with self._lock:
            self.rssi_buffer.append(rssi)
            self.timestamps.append(now)
            self._packet_count += 1

        self._detect_movement(now)
        self._update_direction_speed()

        if self._db_enabled and now - self._last_db_write >= DB_WRITE_INTERVAL:
            self._persist_to_db()
            self._last_db_write = now

        if self.occupancy_tracking and now - self._last_occ_check >= OCCUPANCY_CHECK_S:
            self._update_occupancy(now)
            self._last_occ_check = now

        if self.breathing_detection and now - self._breath_check_ts >= 5.0:
            self._breath_check_ts = now
            threading.Thread(target=self._detect_micro_motion, daemon=True).start()

    # ------------------------------------------------------------------
    # Movement detection
    # ------------------------------------------------------------------

    def _is_interference(self, now: float) -> bool:
        if not self.fp_filter:
            return False
        with self._lock:
            recent = [t for t in self._spike_times if t > now - 1.0]
        return len(recent) >= INTERFERENCE_SPIKE_RATE

    def _detect_movement(self, now: float) -> None:
        with self._lock:
            samples = list(self.rssi_buffer)
            n = len(samples)

        if n < BASELINE_SAMPLES:
            return

        if not self._calibrated:
            self._calibrated = True
            print("[+] Calibration complete. Monitoring for movement...\n")

        smoothed = uniform_filter1d(samples, size=SMOOTHING_WINDOW)
        baseline = smoothed[:-SMOOTHING_WINDOW]
        mean = np.mean(baseline)
        std  = max(float(np.std(baseline)), 0.5)
        current = float(smoothed[-1])
        z_score = abs(current - mean) / std

        if z_score > self.sensitivity:
            with self._lock:
                self._spike_times.append(now)

        if self._is_interference(now):
            return  # suppress — looks like microwave or other periodic interference

        fall_z = self.sensitivity * self.fall_multiplier
        with self._lock:
            last_fall = self._last_fall_alert
        if z_score > fall_z and (now - last_fall) > 10.0:
            threading.Timer(
                FALL_SILENCE_S, self._check_fall_confirmation, args=(now, z_score)
            ).start()

        if z_score > self.sensitivity and (now - self._last_alert) > ALERT_COOLDOWN:
            self._last_alert = now
            event = {"ts": now, "z_score": round(z_score, 2), "rssi": round(current, 1)}
            with self._lock:
                self.movement_events.append(event)
                if len(self.movement_events) > 500:
                    self.movement_events = self.movement_events[-500:]
            print(f"[!] MOVEMENT  RSSI: {current:.1f} dBm  z: {z_score:.2f}  "
                  f"dir: {self._direction}  speed: {self._speed}")
            self._check_alert_zones(current, now)
            self._fire_ha("movement", z_score, current)
            if self._db_enabled:
                db.record_event(now, "movement", z_score, f"rssi={current:.1f}")

    def _check_fall_confirmation(self, event_ts: float, peak_z: float) -> None:
        """After silence window: if signal is quiet → likely fall."""
        with self._lock:
            recent = list(self.rssi_buffer)[-30:]
        if len(recent) < 5:
            return
        smoothed = uniform_filter1d(recent, size=min(5, len(recent)))
        std = float(np.std(smoothed))
        now = time.time()
        with self._lock:
            already_alerted = (now - self._last_fall_alert) <= 10.0
        if std < 0.5 and not already_alerted:
            with self._lock:
                self._last_fall_alert = now
            fall = {"ts": event_ts, "z_score": round(peak_z, 2)}
            with self._lock:
                self.fall_events.append(fall)
            print(f"[!!!] POSSIBLE FALL DETECTED  z-score: {peak_z:.2f}")
            self._fire_ha("fall", peak_z, None)
            if self._db_enabled:
                db.record_event(event_ts, "fall", peak_z)

    # ------------------------------------------------------------------
    # Direction and speed
    # ------------------------------------------------------------------

    def _update_direction_speed(self) -> None:
        with self._lock:
            if len(self.rssi_buffer) < DIRECTION_WINDOW + 5:
                return
            recent    = list(self.rssi_buffer)[-DIRECTION_WINDOW:]
            recent_ts = list(self.timestamps)[-DIRECTION_WINDOW:]

        smoothed = uniform_filter1d(recent, size=5)
        t0 = recent_ts[0]
        x  = np.array(recent_ts) - t0
        span = x[-1]
        if span < 0.01:
            return

        slope = float(np.polyfit(x, smoothed, 1)[0])  # dBm/s

        if slope > 0.1:
            direction = "approaching"
        elif slope < -0.1:
            direction = "moving_away"
        else:
            direction = "stationary"

        abs_slope = abs(slope)
        if abs_slope < SPEED_SLOW_DBM_S:
            speed = "stationary"
        elif abs_slope < SPEED_FAST_DBM_S:
            speed = "slow"
        elif abs_slope < SPEED_FAST_DBM_S * 3:
            speed = "medium"
        else:
            speed = "fast"

        with self._lock:
            self._direction = direction
            self._speed     = speed

    # ------------------------------------------------------------------
    # Breathing / micro-motion (FFT)
    # ------------------------------------------------------------------

    def _detect_micro_motion(self) -> None:
        now = time.time()
        cutoff = now - BREATHING_WINDOW_S

        with self._lock:
            ts_raw   = list(self.timestamps)
            rssi_raw = list(self.rssi_buffer)

        pairs = [(t, r) for t, r in zip(ts_raw, rssi_raw) if t >= cutoff]
        if len(pairs) < 60:
            return

        ts_arr   = np.array([p[0] for p in pairs])
        rssi_arr = np.array([p[1] for p in pairs], dtype=float)

        duration = ts_arr[-1] - ts_arr[0]
        if duration < 10:
            return

        n_samples  = int(duration * FFT_SAMPLE_RATE)
        t_regular  = np.linspace(ts_arr[0], ts_arr[-1], n_samples)
        rssi_reg   = np.interp(t_regular, ts_arr, rssi_arr)

        # Detrend to remove slow drift
        trend    = np.polyval(np.polyfit(t_regular, rssi_reg, 1), t_regular)
        detrended = rssi_reg - trend

        # Hann window + FFT
        win     = np.hanning(len(detrended))
        fft_mag = np.abs(np.fft.rfft(detrended * win))
        freqs   = np.fft.rfftfreq(len(detrended), d=1.0 / FFT_SAMPLE_RATE)

        noise_floor = float(np.median(fft_mag))

        def _peak_in_band(f_min, f_max, snr_threshold):
            mask = (freqs >= f_min) & (freqs <= f_max)
            if not mask.any():
                return False, None
            band_mag   = fft_mag[mask]
            band_freqs = freqs[mask]
            peak_idx   = int(np.argmax(band_mag))
            return band_mag[peak_idx] > noise_floor * snr_threshold, float(band_freqs[peak_idx])

        breath_det, breath_hz = _peak_in_band(BREATHING_MIN_HZ, BREATHING_MAX_HZ, 3.0)
        hb_det,     hb_hz     = _peak_in_band(HEARTBEAT_MIN_HZ, HEARTBEAT_MAX_HZ, 5.0)

        with self._lock:
            self._breathing_detected = breath_det
            self._breathing_freq     = round(breath_hz, 3) if breath_det else None
            self._heartbeat_detected = hb_det
            self._heartbeat_freq     = round(hb_hz, 3) if hb_det else None

    # ------------------------------------------------------------------
    # Alert zones
    # ------------------------------------------------------------------

    def _check_alert_zones(self, current_rssi: float, ts: float) -> None:
        with self._lock:
            zones = list(self._alert_zones)
        for zone in zones:
            r_min = zone.get("rssi_min", -100)
            r_max = zone.get("rssi_max", -20)
            if r_min <= current_rssi <= r_max:
                name = zone.get("name", "unnamed")
                print(f"[!] ALERT ZONE '{name}' triggered  RSSI: {current_rssi:.1f}")
                with self._lock:
                    self.zone_alerts.append({"zone": name, "ts": ts, "rssi": current_rssi})
                    if len(self.zone_alerts) > 200:
                        self.zone_alerts = self.zone_alerts[-200:]
                if self._db_enabled:
                    db.record_event(ts, "zone_alert", None, f"zone={name},rssi={current_rssi:.1f}")

    # ------------------------------------------------------------------
    # Home Assistant integration
    # ------------------------------------------------------------------

    def _fire_ha(self, event_type: str, z_score: float | None, rssi: float | None) -> None:
        if not self.ha_webhook_url:
            return
        import json
        import urllib.request
        import urllib.error
        payload = json.dumps({
            "event": event_type,
            "z_score": z_score,
            "rssi": rssi,
            "ts": time.time(),
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.ha_token:
            headers["Authorization"] = f"Bearer {self.ha_token}"
        try:
            req = urllib.request.Request(
                self.ha_webhook_url, data=payload, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            print(f"[-] HA notification failed: {e}")

    # ------------------------------------------------------------------
    # Occupancy tracking
    # ------------------------------------------------------------------

    def _update_occupancy(self, now: float) -> None:
        if not self._calibrated or not self._db_enabled:
            return
        with self._lock:
            # Occupied if there was a movement event in the last 90 seconds
            recent_events = [e for e in self.movement_events if e["ts"] > now - 90]
            is_occupied = bool(recent_events) or self._breathing_detected

        if is_occupied:
            if self._occupancy_start is None:
                self._occupancy_start = now
        else:
            if self._occupancy_start is not None:
                secs = now - self._occupancy_start
                db.record_occupancy(secs)
                self._occupancy_start = None

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    def _persist_to_db(self) -> None:
        with self._lock:
            if not self.rssi_buffer:
                return
            rssi_val = float(self.rssi_buffer[-1])
            ts_val   = float(self.timestamps[-1])
            samples  = list(self.rssi_buffer)
        smoothed_val = float(uniform_filter1d(samples, size=SMOOTHING_WINDOW)[-1])
        db.record_rssi(ts_val, rssi_val, smoothed_val)

    # ------------------------------------------------------------------
    # TX injection — dual adapter active radar
    # ------------------------------------------------------------------

    def start_tx(self) -> None:
        """Inject probe request frames at 10 Hz on the TX interface."""
        if not self.tx_interface:
            return
        print(f"[*] Starting TX probe injection on {self.tx_interface} at 10 Hz...")
        frame = (
            RadioTap() /
            Dot11(type=0, subtype=4,
                  addr1="ff:ff:ff:ff:ff:ff",
                  addr2="12:34:56:78:9a:bc",
                  addr3="ff:ff:ff:ff:ff:ff") /
            Dot11ProbeReq() /
            Dot11Elt(ID="SSID", info="wifi-radar") /
            Dot11Elt(ID="Rates", info=b"\x82\x84\x8b\x96\x0c\x12\x18\x24")
        )
        def _inject():
            while True:
                try:
                    sendp(frame, iface=self.tx_interface, verbose=False)
                    time.sleep(0.1)
                except Exception as e:
                    print(f"[-] TX error: {e}")
                    time.sleep(1.0)
        threading.Thread(target=_inject, daemon=True).start()
        print(f"[+] TX injection active on {self.tx_interface}")

    # ------------------------------------------------------------------
    # Sniffing — auto-restart on error
    # ------------------------------------------------------------------

    def start_sniffing(self) -> None:
        def _sniff():
            while True:
                try:
                    sniff(
                        iface=self.interface,
                        prn=self.packet_callback,
                        store=False,
                        filter="type mgt or type data",
                    )
                except Exception as e:
                    print(f"[-] Sniff error ({e}), restarting in 2s...")
                    time.sleep(2)
                    try:
                        restart_interface(self.interface, self.channel)
                    except Exception:
                        pass

        threading.Thread(target=_sniff, daemon=True).start()
        print(f"[+] Sniffing on {self.interface} — press Ctrl+C to stop\n")

    # ------------------------------------------------------------------
    # State export
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        with self._lock:
            rssi       = list(self.rssi_buffer)
            timestamps = list(self.timestamps)
            events     = list(self.movement_events)
            falls      = list(self.fall_events)
            count      = self._packet_count
            calibrated = self._calibrated
            direction  = self._direction
            speed      = self._speed
            breathing  = self._breathing_detected
            b_hz       = self._breathing_freq
            heartbeat  = self._heartbeat_detected
            hb_hz      = self._heartbeat_freq
            z_alerts   = list(self.zone_alerts[-20:])

        return {
            "rssi":              rssi[-500:],
            "timestamps":        timestamps[-500:],
            "movement_events":   events[-50:],
            "fall_events":       falls[-10:],
            "packet_count":      count,
            "calibrated":        calibrated,
            "current_rssi":      rssi[-1] if rssi else None,
            "heatmap":           self._build_heatmap(rssi, timestamps),
            "direction":         direction,
            "speed":             speed,
            "breathing_detected": breathing,
            "breathing_freq_hz": b_hz,
            "heartbeat_detected": heartbeat,
            "heartbeat_freq_hz": hb_hz,
            "zone_alerts":       z_alerts,
        }

    def _build_heatmap(
        self, rssi: list, timestamps: list,
        window: int = 120, bin_sec: int = 1,
        rssi_min: int = -100, rssi_max: int = -20, rssi_step: int = 5,
    ) -> dict:
        now    = time.time()
        cutoff = now - window
        n_time = window // bin_sec
        n_rssi = (rssi_max - rssi_min) // rssi_step
        matrix = [[0] * n_time for _ in range(n_rssi)]

        for ts, r in zip(timestamps, rssi):
            if ts < cutoff:
                continue
            t_idx = min(int((ts - cutoff) / bin_sec), n_time - 1)
            r_idx = min(int((r - rssi_min) / rssi_step), n_rssi - 1)
            if 0 <= t_idx < n_time and 0 <= r_idx < n_rssi:
                matrix[r_idx][t_idx] += 1

        y_labels = [str(rssi_min + i * rssi_step) for i in range(n_rssi)]
        return {"matrix": matrix, "y_labels": y_labels, "n_time": n_time, "window": window}

    # ------------------------------------------------------------------
    # Live matplotlib plot (standalone mode)
    # ------------------------------------------------------------------

    def run_plot(self) -> None:
        fig, (ax_rssi, ax_event) = plt.subplots(2, 1, figsize=(12, 6),
                                                  gridspec_kw={"height_ratios": [3, 1]})
        fig.suptitle("Wi-Fi Radar — Signal Disruption Monitor", fontsize=13)

        line_raw,    = ax_rssi.plot([], [], color="steelblue", alpha=0.5, linewidth=0.8, label="Raw RSSI")
        line_smooth, = ax_rssi.plot([], [], color="royalblue", linewidth=1.5, label="Smoothed")
        line_mean,   = ax_rssi.plot([], [], color="gray", linewidth=1, linestyle="--", label="Baseline")

        ax_rssi.set_ylabel("RSSI (dBm)")
        ax_rssi.set_ylim(-100, -20)
        ax_rssi.legend(loc="upper right", fontsize=8)
        ax_rssi.grid(True, alpha=0.3)

        ax_event.set_ylabel("Movement")
        ax_event.set_ylim(-0.5, 1.5)
        ax_event.set_yticks([0, 1])
        ax_event.set_yticklabels(["Clear", "Detected"])
        ax_event.grid(True, alpha=0.3)

        status_text = ax_rssi.text(
            0.01, 0.95, "", transform=ax_rssi.transAxes, fontsize=9,
            verticalalignment="top", color="white",
            bbox=dict(boxstyle="round", facecolor="steelblue", alpha=0.7),
        )

        def update(_frame):
            with self._lock:
                samples = list(self.rssi_buffer)
                events  = list(self.movement_events)
                count   = self._packet_count
                direction = self._direction
                speed     = self._speed
                breathing = self._breathing_detected

            if len(samples) < 2:
                return line_raw, line_smooth, line_mean

            x        = list(range(len(samples)))
            smoothed = uniform_filter1d(samples, size=SMOOTHING_WINDOW).tolist()
            mean_val = float(np.mean(samples[: max(1, len(samples) - SMOOTHING_WINDOW)]))

            line_raw.set_data(x, samples)
            line_smooth.set_data(x, smoothed)
            line_mean.set_data([0, len(samples) - 1], [mean_val, mean_val])
            ax_rssi.set_xlim(0, max(BUFFER_SIZE, len(samples)))

            ax_event.cla()
            ax_event.set_ylabel("Movement")
            ax_event.set_ylim(-0.5, 1.5)
            ax_event.set_yticks([0, 1])
            ax_event.set_yticklabels(["Clear", "Detected"])
            ax_event.set_xlim(0, max(BUFFER_SIZE, len(samples)))
            ax_event.grid(True, alpha=0.3)
            ax_event.axhline(0, color="green", alpha=0.4)

            with self._lock:
                ts_list = list(self.timestamps)
            for evt in events[-20:]:
                evt_ts = evt["ts"] if isinstance(evt, dict) else evt
                if ts_list:
                    idx = min(range(len(ts_list)), key=lambda i: abs(ts_list[i] - evt_ts))
                    ax_event.axvline(idx, color="red", alpha=0.7, linewidth=1.5)

            cal_str = "Calibrated" if self._calibrated else f"Calibrating ({len(samples)}/{BASELINE_SAMPLES})"
            extra = f"  dir:{direction}  spd:{speed}"
            if breathing:
                extra += "  [breathing]"
            status_text.set_text(f"Packets:{count}  {cal_str}  Events:{len(events)}{extra}")
            return line_raw, line_smooth, line_mean

        ani = animation.FuncAnimation(fig, update, interval=200, blit=False, cache_frame_data=False)  # noqa: F841
        plt.tight_layout()
        try:
            plt.show()
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Wi-Fi Radar — movement detection")
    parser.add_argument("--interface", "-i", required=True)
    parser.add_argument("--tx-interface", "-t", default=None, help="Second adapter for active TX mode")
    parser.add_argument("--channel", "-c", type=int, default=None)
    parser.add_argument("--sensitivity", "-s", type=float, default=MOVEMENT_THRESHOLD)
    parser.add_argument("--no-monitor-setup", action="store_true")
    parser.add_argument("--no-db", action="store_true", help="Disable SQLite history")
    args = parser.parse_args()

    if not args.no_monitor_setup:
        set_monitor_mode(args.interface, args.channel)
        if args.tx_interface:
            set_monitor_mode(args.tx_interface, args.channel)

    cfg = cfg_mod.load() if _HAS_MODULES else {}
    radar = WifiRadar(
        interface=args.interface,
        sensitivity=args.sensitivity,
        tx_interface=args.tx_interface,
        channel=args.channel,
        ha_webhook_url=cfg.get("ha_webhook_url"),
        ha_token=cfg.get("ha_token"),
        use_db=not args.no_db,
    )

    try:
        if args.tx_interface:
            radar.start_tx()
        radar.start_sniffing()
        radar.run_plot()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_monitor_setup:
            restore_managed_mode(args.interface)
            if args.tx_interface:
                restore_managed_mode(args.tx_interface)


if __name__ == "__main__":
    main()
