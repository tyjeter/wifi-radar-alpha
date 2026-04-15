#!/usr/bin/env python3
"""
watchdog.py — Keeps Wi-Fi Radar running unattended.

Monitors:
  - dashboard.py process (restarts if it dies)
  - WiFi adapter packet rate (restarts sniffing if it stalls)
  - cloudflared tunnel (restarts if it dies)

Usage:
    sudo python3 watchdog.py [--config ~/.wifi_radar_config.json]
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    import config as cfg_mod
    _cfg = cfg_mod.load()
except ImportError:
    _cfg = {}

STALL_THRESHOLD_S = 30      # seconds without new packets = adapter stalled
CHECK_INTERVAL_S  = 10      # how often to check all processes
RESTART_DELAY_S   = 3       # seconds to wait before restarting a dead process
LOG_FILE          = os.path.expanduser("~/.wifi_radar_watchdog.log")
LOG_MAX_BYTES     = 5 * 1024 * 1024   # 5 MB — truncate oldest half when exceeded


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            with open(LOG_FILE, "rb") as f:
                content = f.read()
            with open(LOG_FILE, "wb") as f:
                f.write(content[len(content) // 2:])
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _find_tunnel_url(proc: subprocess.Popen) -> str | None:
    """Parse cloudflared stdout for the tunnel URL."""
    pat = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')
    for line in proc.stdout:
        m = pat.search(line)
        if m:
            return m.group(0)
    return None


class Watchdog:
    def __init__(
        self,
        interface:    str,
        port:         int,
        channel:      int | None,
        sensitivity:  float,
        tx_interface: str | None,
        vercel_url:   str | None,
        auto_tunnel:  bool,
    ):
        self.interface    = interface
        self.port         = port
        self.channel      = channel
        self.sensitivity  = sensitivity
        self.tx_interface = tx_interface
        self.vercel_url   = vercel_url
        self.auto_tunnel  = auto_tunnel

        self._dashboard_proc:   subprocess.Popen | None = None
        self._agent_proc:       subprocess.Popen | None = None
        self._cloudflared_proc: subprocess.Popen | None = None
        self._tunnel_url:       str | None = None
        self._last_packet_count = 0
        self._last_count_ts     = time.time()
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

    def _handle_signal(self, *_):
        log("Watchdog stopping...")
        self._running = False
        self._kill_all()
        sys.exit(0)

    def _kill_all(self):
        for proc in (self._dashboard_proc, self._agent_proc, self._cloudflared_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Dashboard process
    # ------------------------------------------------------------------

    def _dashboard_cmd(self) -> list[str]:
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [
            sys.executable, os.path.join(here, "dashboard.py"),
            "--interface", self.interface,
            "--port", str(self.port),
            "--sensitivity", str(self.sensitivity),
        ]
        if self.channel:
            cmd += ["--channel", str(self.channel)]
        if self.tx_interface:
            cmd += ["--tx-interface", self.tx_interface]
        return cmd

    def start_dashboard(self) -> None:
        log(f"Starting dashboard on port {self.port}...")
        self._dashboard_proc = subprocess.Popen(
            self._dashboard_cmd(),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        log(f"Dashboard PID {self._dashboard_proc.pid}")

    def _check_dashboard(self) -> None:
        if self._dashboard_proc is None or self._dashboard_proc.poll() is not None:
            rc = self._dashboard_proc.poll() if self._dashboard_proc else "none"
            log(f"Dashboard died (rc={rc}), restarting in {RESTART_DELAY_S}s...")
            time.sleep(RESTART_DELAY_S)
            self.start_dashboard()
            return

        # Check if adapter has stalled (no packets for STALL_THRESHOLD_S)
        try:
            resp = urllib.request.urlopen(
                f"http://localhost:{self.port}/api/state", timeout=5
            )
            state = json.loads(resp.read())
            pkt = state.get("packet_count", 0)
            now = time.time()
            if pkt == self._last_packet_count and (now - self._last_count_ts) > STALL_THRESHOLD_S:
                log("Adapter appears stalled (no new packets). Restarting interface...")
                self._restart_adapter()
            elif pkt != self._last_packet_count:
                self._last_packet_count = pkt
                self._last_count_ts     = now
        except Exception:
            pass  # dashboard might be starting up

    def _restart_adapter(self) -> None:
        iface = self.interface
        log(f"Restarting adapter {iface}...")
        subprocess.run(["ip", "link", "set", iface, "down"], capture_output=True)
        time.sleep(1)
        subprocess.run(["ip", "link", "set", iface, "up"], capture_output=True)
        if self.channel:
            subprocess.run(["iw", iface, "set", "channel", str(self.channel)], capture_output=True)
        self._last_count_ts = time.time()
        log("Adapter restarted.")

    # ------------------------------------------------------------------
    # cloudflared tunnel
    # ------------------------------------------------------------------

    def start_tunnel(self) -> str | None:
        if not self.auto_tunnel:
            return None
        log(f"Starting cloudflared tunnel → port {self.port}...")
        self._cloudflared_proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{self.port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = _find_tunnel_url(self._cloudflared_proc)
        if url:
            log(f"Tunnel URL: {url}")
            self._tunnel_url = url
        else:
            log("Warning: could not parse tunnel URL from cloudflared output.")
        return url

    def _check_tunnel(self) -> None:
        if not self.auto_tunnel:
            return
        if self._cloudflared_proc is None or self._cloudflared_proc.poll() is not None:
            rc = self._cloudflared_proc.poll() if self._cloudflared_proc else "none"
            log(f"cloudflared died (rc={rc}), restarting...")
            time.sleep(RESTART_DELAY_S)
            new_url = self.start_tunnel()
            if new_url and new_url != self._tunnel_url:
                log(f"New tunnel URL: {new_url}")
                self._tunnel_url = new_url
                # Notify agent of new URL so it re-registers
                if self._agent_proc and self._agent_proc.poll() is None:
                    self._agent_proc.terminate()
                    self._start_agent()

    # ------------------------------------------------------------------
    # Pi registration agent
    # ------------------------------------------------------------------

    def _start_agent(self) -> None:
        if not self.vercel_url or not self._tunnel_url:
            return
        here = os.path.dirname(os.path.abspath(__file__))
        agent = os.path.join(here, "pi-agent", "register.py")
        if not os.path.exists(agent):
            return
        log(f"Starting Pi agent → {self.vercel_url}")
        self._agent_proc = subprocess.Popen(
            [sys.executable, agent,
             "--vercel-url", self.vercel_url,
             "--tunnel-url", self._tunnel_url],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    def _check_agent(self) -> None:
        if not self.vercel_url or not self._tunnel_url:
            return
        if self._agent_proc is None or self._agent_proc.poll() is not None:
            rc = self._agent_proc.poll() if self._agent_proc else "none"
            log(f"Pi agent died (rc={rc}), restarting...")
            time.sleep(RESTART_DELAY_S)
            self._start_agent()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        log("=== Wi-Fi Radar Watchdog started ===")
        self.start_dashboard()
        time.sleep(5)   # give dashboard time to start

        if self.auto_tunnel:
            self.start_tunnel()
            time.sleep(3)

        if self.vercel_url and self._tunnel_url:
            self._start_agent()

        while self._running:
            self._check_dashboard()
            self._check_tunnel()
            self._check_agent()
            time.sleep(CHECK_INTERVAL_S)


def main():
    parser = argparse.ArgumentParser(description="Wi-Fi Radar watchdog")
    parser.add_argument("--interface",    "-i", default=_cfg.get("interface", "wlan0"))
    parser.add_argument("--port",         "-p", type=int, default=_cfg.get("port", 5000))
    parser.add_argument("--channel",      "-c", type=int, default=_cfg.get("channel"))
    parser.add_argument("--sensitivity",  "-s", type=float, default=_cfg.get("sensitivity", 2.0))
    parser.add_argument("--tx-interface",       default=_cfg.get("tx_interface"))
    parser.add_argument("--vercel-url",         default=_cfg.get("vercel_url"))
    parser.add_argument("--auto-tunnel",        action="store_true",
                        default=bool(_cfg.get("auto_tunnel", False)))
    args = parser.parse_args()

    wd = Watchdog(
        interface=args.interface,
        port=args.port,
        channel=args.channel,
        sensitivity=args.sensitivity,
        tx_interface=args.tx_interface,
        vercel_url=args.vercel_url,
        auto_tunnel=args.auto_tunnel,
    )
    wd.run()


if __name__ == "__main__":
    main()
