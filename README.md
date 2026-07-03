# Julice Camera Trap

Deployment source for the Julice July 2026 Raspberry Pi Zero 2 W station.

This build has no image system: no camera, no motion detector, no flash, no image AI, no photo folders, and no photo CSV columns. It logs GPS/RTC time, BME280, VEML7700 lux, CPU temperature, USB microphone recordings processed by BirdNET, and Google Drive CSV sync.

## Field Behavior

- Writes one main CSV at `/mnt/juara_usb/Julice Camera Trap July 2026.csv`.
- Records five-minute audio intervals, processes them with BirdNET, then deletes the WAV files from the Pi.
- Never stores audio on the USB drive.
- Logs BME280 temperature, humidity, pressure in mmHg, lux, CPU temperature, GPS/fallback coordinates, BirdNET diversity metrics, and up to 90 individual call cells.
- Uses SQLite as the source of truth and atomically rebuilds the CSV after updates.
- Keeps running if GPS, RTC, BME280, lux, mic, internet, USB, or Google Drive temporarily fail.
- Rebuilds the active BirdNET species list from GPS after boot when 10 consistent GPS fixes are available; otherwise it uses saved or fallback coordinates.
- Uploads the CSV to Google Drive folder `Julice Camera Trap July 2026` whenever the CSV changes and every five minutes while online.
- If internet is unavailable, Drive sync exits cleanly and the next timer or CSV update retries.

## BirdNET Settings

- BirdNET Analyzer: `2.4.0`.
- Generated GPS species list by default: `birdnet.species_filter_mode = "generated_list"`.
- 100 mile x 100 mile species cells, nearest 4 cells unioned into one active list.
- Primary confidence threshold: `0.25`.
- Candidate/alternate species threshold: `0.10`.
- Audio gain before analysis: `36.0 dB`.
- Day sensitivity: `1.0`; night sensitivity: `0.8`.
- Day overlap: `0.0`; night overlap: `0.0`.
- One worker, batch size 1, one file per worker cycle.
- Fast TFLite path enabled.

## Install On A Pi

The SD card should already be written by Raspberry Pi Imager to join:

- SSID: `JULICE`
- Password: `EROS2016`

On the Pi:

```bash
git clone https://github.com/esmaby444/Julice-Camera-Trap.git ~/Julice-Camera-Trap
cd ~/Julice-Camera-Trap
sudo scripts/install_julice_camera_trap.sh
```

Then do the one-time Google Drive login as the station user:

```bash
sudo -u "$USER" /usr/local/bin/juara_gdrive_auth_helper
sudo -u "$USER" rclone config reconnect juara-gdrive:
sudo -u "$USER" /usr/local/bin/juara_gdrive_sync
```

After setup:

```bash
sudo scripts/pi_preflight.sh
sudo systemctl start juara-station
sudo systemctl start juara-ai-worker
```

## Useful Commands

```bash
sudo journalctl -u juara-station -f
sudo journalctl -u juara-ai-worker -f
sudo journalctl -u juara-gdrive-sync.service -n 80 --no-pager
juara-station --config /etc/juara-station.toml doctor
juara-station --config /etc/juara-station.toml export-csv
```

## Local Smoke Test

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/juara-station --mock --config configs/local.mock.toml once --duration 1
.venv/bin/pytest
```
