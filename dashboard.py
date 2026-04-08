#!/usr/bin/env python3
"""
dashboard.py — Web dashboard for Wi-Fi Radar.

Usage:
    sudo python3 dashboard.py --interface wlan0
    sudo python3 dashboard.py --interface wlan0 --channel 6 --port 5000
"""

import argparse
import json
import sys
import time

from flask import Flask, Response, render_template

from wifi_radar import WifiRadar, set_monitor_mode, restore_managed_mode, MOVEMENT_THRESHOLD

app = Flask(__name__)
radar: WifiRadar | None = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint — pushes radar state every 500ms."""
    def generate():
        while True:
            try:
                state = radar.get_state()
                yield f"data: {json.dumps(state)}\n\n"
            except Exception:
                yield "data: {}\n\n"
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main():
    global radar

    parser = argparse.ArgumentParser(description="Wi-Fi Radar — web dashboard")
    parser.add_argument("--interface", "-i", required=True, help="Wi-Fi interface (e.g. wlan0)")
    parser.add_argument("--channel", "-c", type=int, default=None, help="Lock to a specific channel (1-13)")
    parser.add_argument("--sensitivity", "-s", type=float, default=MOVEMENT_THRESHOLD,
                        help=f"Detection sensitivity (default: {MOVEMENT_THRESHOLD})")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Web server port (default: 5000)")
    parser.add_argument("--no-monitor-setup", action="store_true",
                        help="Skip monitor mode setup if already configured")
    args = parser.parse_args()

    if not args.no_monitor_setup:
        set_monitor_mode(args.interface, args.channel)

    radar = WifiRadar(args.interface, sensitivity=args.sensitivity)

    try:
        radar.start_sniffing()
        print(f"[+] Dashboard running at http://localhost:{args.port}")
        app.run(host="0.0.0.0", port=args.port, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_monitor_setup:
            restore_managed_mode(args.interface)


if __name__ == "__main__":
    main()
