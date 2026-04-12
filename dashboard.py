#!/usr/bin/env python3
"""
dashboard.py — Web dashboard for Wi-Fi Radar.

Routes:
  GET  /                  Main dashboard UI
  GET  /stream            SSE — live radar state every 500ms
  GET  /api/state         Same state as JSON (for playback)
  GET  /api/history       Historical RSSI/events — ?since=&until= (unix ts)
  GET  /api/occupancy     Daily occupancy hours (last 30 days)
  GET  /export/csv        Download CSV of RSSI history
  GET  /config            Web config page
  POST /config            Save config (JSON body)
  POST /api/zones         Update alert zones [{name,rssi_min,rssi_max}]
  POST /api/ha-test       Test HA webhook — fires a test event

Usage:
    sudo python3 dashboard.py --interface wlan0
    sudo python3 dashboard.py --interface wlan0 --channel 6 --port 5000
"""

import argparse
import json
import sys
import threading
import time

from flask import Flask, Response, render_template, request, jsonify

import config as cfg_mod
import db
from wifi_radar import WifiRadar, set_monitor_mode, restore_managed_mode, MOVEMENT_THRESHOLD

app = Flask(__name__)
radar: WifiRadar | None = None
_cfg: dict = {}


# ---------------------------------------------------------------------------
# Live stream
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", device_name=_cfg.get("device_name", "Wi-Fi Radar"))


@app.route("/stream")
def stream():
    def generate():
        try:
            while True:
                try:
                    state = radar.get_state()
                    yield f"data: {json.dumps(state)}\n\n"
                except Exception:
                    yield "data: {}\n\n"
                time.sleep(0.5)
        except GeneratorExit:
            pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/state")
def api_state():
    return jsonify(radar.get_state())


# ---------------------------------------------------------------------------
# History / playback
# ---------------------------------------------------------------------------

@app.route("/api/history")
def api_history():
    try:
        since = float(request.args.get("since", time.time() - 3600))
        until = float(request.args.get("until", time.time()))
        limit = int(request.args.get("limit", 5000))
    except ValueError:
        return jsonify({"error": "invalid params"}), 400

    rssi_data = db.get_history(since=since, until=until, limit=limit)
    events    = db.get_events(since=since, until=until)
    return jsonify({"rssi": rssi_data, "events": events, "since": since, "until": until})


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------

@app.route("/api/occupancy")
def api_occupancy():
    days = int(request.args.get("days", 30))
    return jsonify(db.get_occupancy(days=days))


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@app.route("/export/csv")
def export_csv():
    since = float(request.args.get("since", time.time() - 86400))
    until = float(request.args.get("until", time.time()))
    csv_data = db.export_csv(since=since, until=until)
    filename = time.strftime("wifi_radar_%Y%m%d_%H%M%S.csv")
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@app.route("/config", methods=["GET"])
def config_page():
    return render_template("config.html", cfg=_cfg)


@app.route("/config", methods=["POST"])
def config_save():
    global _cfg
    try:
        updates = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    # Sanitise numeric fields
    for float_key in ("sensitivity", "fall_threshold_multiplier"):
        if float_key in updates:
            try:
                updates[float_key] = float(updates[float_key])
            except (ValueError, TypeError):
                updates.pop(float_key)
    for int_key in ("channel", "port", "db_retention_days"):
        if int_key in updates and updates[int_key] is not None:
            try:
                updates[int_key] = int(updates[int_key])
            except (ValueError, TypeError):
                updates.pop(int_key)

    _cfg = cfg_mod.update(updates)

    # Apply live changes that don't require restart
    if "sensitivity" in updates:
        radar.set_sensitivity(updates["sensitivity"])
    if "alert_zones" in updates:
        radar.set_alert_zones(updates["alert_zones"])
    if "ha_webhook_url" in updates:
        radar.ha_webhook_url = updates["ha_webhook_url"]
    if "ha_token" in updates:
        radar.ha_token = updates["ha_token"]

    return jsonify({"ok": True, "cfg": _cfg})


# ---------------------------------------------------------------------------
# Alert zones
# ---------------------------------------------------------------------------

@app.route("/api/zones", methods=["GET"])
def api_zones_get():
    return jsonify(_cfg.get("alert_zones", []))


@app.route("/api/zones", methods=["POST"])
def api_zones():
    try:
        zones = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400
    if not isinstance(zones, list):
        return jsonify({"error": "expected array"}), 400
    radar.set_alert_zones(zones)
    cfg_mod.update({"alert_zones": zones})
    return jsonify({"ok": True, "zones": zones})


# ---------------------------------------------------------------------------
# HA test
# ---------------------------------------------------------------------------

@app.route("/api/ha-test", methods=["POST"])
def api_ha_test():
    if not radar.ha_webhook_url:
        return jsonify({"error": "ha_webhook_url not configured"}), 400
    radar._fire_ha("test", None, None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global radar, _cfg

    parser = argparse.ArgumentParser(description="Wi-Fi Radar — web dashboard")
    parser.add_argument("--interface", "-i", default=None)
    parser.add_argument("--tx-interface", "-t", default=None)
    parser.add_argument("--channel", "-c", type=int, default=None)
    parser.add_argument("--sensitivity", "-s", type=float, default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--no-monitor-setup", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    _cfg = cfg_mod.load()

    # CLI args override config file
    interface    = args.interface    or _cfg.get("interface", "wlan0")
    tx_interface = args.tx_interface or _cfg.get("tx_interface")
    channel      = args.channel      if args.channel is not None else _cfg.get("channel")
    sensitivity  = args.sensitivity  if args.sensitivity is not None else _cfg.get("sensitivity", MOVEMENT_THRESHOLD)
    port         = args.port         if args.port is not None else _cfg.get("port", 5000)

    if not args.no_monitor_setup:
        set_monitor_mode(interface, channel)
        if tx_interface:
            set_monitor_mode(tx_interface, channel)

    radar = WifiRadar(
        interface=interface,
        sensitivity=sensitivity,
        tx_interface=tx_interface,
        channel=channel,
        ha_webhook_url=_cfg.get("ha_webhook_url"),
        ha_token=_cfg.get("ha_token"),
        fall_multiplier=_cfg.get("fall_threshold_multiplier", 3.0),
        breathing_detection=_cfg.get("breathing_detection", True),
        false_positive_filter=_cfg.get("false_positive_filter", True),
        occupancy_tracking=_cfg.get("occupancy_tracking", True),
        use_db=not args.no_db,
    )
    radar.set_alert_zones(_cfg.get("alert_zones", []))

    # Prune old data now, then once per hour in the background.
    retention_days = _cfg.get("db_retention_days", 7)
    db.prune_old_data(retention_days)

    def _prune_loop():
        while True:
            time.sleep(3600)
            try:
                db.prune_old_data(_cfg.get("db_retention_days", 7))
            except Exception:
                pass

    threading.Thread(target=_prune_loop, daemon=True).start()

    try:
        if tx_interface:
            radar.start_tx()
        radar.start_sniffing()
        print(f"[+] Dashboard at http://0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_monitor_setup:
            restore_managed_mode(interface)
            if tx_interface:
                restore_managed_mode(tx_interface)


if __name__ == "__main__":
    main()
