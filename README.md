# wifi-radar

Detect movement and presence by monitoring disruptions in Wi-Fi signals — no camera, no special sensor, just a Wi-Fi adapter.

## Abstract

Objects moving through a space reflect and absorb Wi-Fi signals, causing measurable changes in signal strength (RSSI). This tool captures raw packets in monitor mode, tracks RSSI over time, and flags statistically significant disruptions as movement events.

## Requirements

**Hardware**
- A Wi-Fi adapter that supports **monitor mode** (e.g. Alfa AWUS036ACH)
- A Linux machine or Raspberry Pi

**Software**
```sh
pip install -r requirements.txt
```

Also requires `iw` and `ip` to be installed (standard on most Linux distros).

## Usage

```sh
sudo python3 wifi_radar.py --interface wlan0
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--interface` / `-i` | Wi-Fi interface name | required |
| `--channel` / `-c` | Lock to a specific channel (1-13) | all channels |
| `--sensitivity` / `-s` | Z-score threshold (lower = more sensitive) | 2.0 |
| `--no-monitor-setup` | Skip monitor mode setup if already configured | off |

**Examples:**
```sh
# Basic usage
sudo python3 wifi_radar.py -i wlan0

# Lock to channel 6, higher sensitivity
sudo python3 wifi_radar.py -i wlan0 --channel 6 --sensitivity 1.5

# Already in monitor mode
sudo python3 wifi_radar.py -i wlan0mon --no-monitor-setup
```

## What you'll see

A real-time plot with two panels:
- **Top**: Raw and smoothed RSSI signal over time, with a baseline mean line
- **Bottom**: Movement events marked as red vertical lines

Movement alerts are also printed to the terminal:
```
[!] MOVEMENT DETECTED  |  RSSI: -62.3 dBm  |  z-score: 3.41
```

## Tips

- Place the adapter in the middle of the area you want to monitor
- A second device (phone, laptop) transmitting nearby improves detection accuracy
- Lower `--sensitivity` if you're missing movement; raise it to reduce false positives
- Locking to a single channel (`--channel`) reduces noise significantly

## Limitations

- RSSI-only detection — good for presence/movement, not precise location (a baseline is required and cannot measure objects that are stationary)
- Stationary objects are not detectable (only movement disrupts the signal)
- Works best in a controlled environment with a stable baseline signal :)

> **Note:** Further testing on limitations and accuracy is ongoing and will be published in future releases.
