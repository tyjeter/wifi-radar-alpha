#!/usr/bin/env python3
"""config.py — Load / save Wi-Fi Radar configuration from ~/.wifi_radar_config.json."""

import json
from pathlib import Path

CONFIG_PATH = Path("~/.wifi_radar_config.json").expanduser()

DEFAULTS: dict = {
    # Hardware
    "interface": "wlan0",
    "tx_interface": None,           # Second adapter for active TX mode
    "channel": None,
    # Detection
    "sensitivity": 2.0,
    "smoothing_window": 5,
    "fall_threshold_multiplier": 3.0,
    "breathing_detection": True,
    "false_positive_filter": True,
    # Alerts
    "alert_zones": [],              # [{name, rssi_min, rssi_max}]
    # Home Assistant
    "ha_webhook_url": None,
    "ha_token": None,
    # Dashboard
    "port": 5000,
    "device_name": None,
    # Vercel hub
    "vercel_url": None,
    "auto_tunnel": False,
    # Data
    "occupancy_tracking": True,
    "db_retention_days": 7,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def update(updates: dict) -> dict:
    cfg = load()
    cfg.update(updates)
    save(cfg)
    return cfg
