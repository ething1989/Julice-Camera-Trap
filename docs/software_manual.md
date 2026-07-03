# Julice Software Manual

## Hardware

- Raspberry Pi Zero 2 W.
- DS3231 RTC on I2C.
- GPS on Pi serial pins through `gpsd` / `gpspipe`.
- BME280 on I2C.
- VEML7700 lux sensor on I2C.
- USB thumb drive mounted at `/mnt/juara_usb`.
- USB microphone recorded with `arecord`.

There is intentionally no camera stack in this repository.

## Storage

SQLite lives at `/var/lib/juara-station/state/station.sqlite3`.

The CSV export lives at:

```text
/mnt/juara_usb/Julice Camera Trap July 2026.csv
```

Temporary WAV files live on the SD card at:

```text
/var/lib/juara-station/audio_recordings
```

The service deletes each WAV after BirdNET finishes. Cleanup also removes stale or interrupted audio files after reboot.

## Main Services

- `juara-station.service`: records audio, samples sensors, writes SQLite, exports CSV.
- `juara-ai-worker.service`: processes pending audio with BirdNET, refreshes affected CSV rows, deletes WAV files.
- `juara-gdrive-sync.timer`: starts Drive sync every five minutes.
- `juara-gdrive-sync.service`: copies CSV files to Google Drive and exits cleanly if offline or unauthenticated.
- `juara-daily-reboot.timer`: planned reboots at the deployed schedule.

## Time And Coordinates

The station asks GPS for time first. It falls back to RTC, then estimated time from the last known timestamp plus one interval if both time sources fail.

Coordinates are selected at startup from 10 consistent GPS fixes. If GPS is unavailable, the station uses the last accepted coordinates; if none exist, it uses backup deployment coordinates:

```text
-16.68260, -56.90453
```

When GPS coordinates become available after boot, a background retry worker rebuilds the active BirdNET species list and exports the CSV again.

## CSV

The Julice CSV profile includes:

- Timestamp, time source, Pi event.
- Temperature, humidity, lux, pressure in mmHg, CPU temperature.
- Latitude and longitude used for BirdNET species filtering.
- Bird diversity metrics.
- Top bird formatted as `Species(Calls: #, Conf: #%)`.
- Audio status.
- `Call 1` through `Call 90`.
- Errors.

Each call cell may contain multiple species candidates separated by new lines, ordered by confidence. Candidates below 10% confidence are ignored. If more than 90 calls are detected in one interval, the strongest 90 calls are retained.

## Google Drive

Drive sync uses `rclone` remote `juara-gdrive` and folder:

```text
Julice Camera Trap July 2026
```

CSV exports call:

```text
systemctl start --no-block juara-gdrive-sync.service
```

That means the station never waits on internet during the logging loop.

## Fail-Safes

- Sensor failures become blank CSV fields and error entries.
- USB failure uses fallback local storage and can request a reboot if the USB remains missing.
- Interrupted audio is recovered or purged on the next boot.
- SQLite WAL mode and atomic CSV replacement protect against power loss during writes.
- Audio is paused during the configured overnight catch-up window.
- Pending audio older than the configured purge cutoff is deleted so the Pi cannot get permanently clogged.
- High CPU temperature triggers cooldown behavior and stops the AI worker.
