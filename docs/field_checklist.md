# Field Checklist

1. Confirm the Pi boots and joins `JULICE`.
2. Confirm `/mnt/juara_usb` is mounted and writable.
3. Run `sudo scripts/pi_preflight.sh`.
4. Confirm I2C shows RTC plus BME280/lux addresses.
5. Confirm `gpspipe -w -n 10` returns GPS messages.
6. Confirm `arecord -l` sees the USB microphone.
7. Confirm Google Drive login with `sudo -u "$USER" rclone about juara-gdrive:`.
8. Start services:

```bash
sudo systemctl restart juara-station juara-ai-worker
```

9. Watch logs for a few minutes:

```bash
sudo journalctl -u juara-station -f
```

10. Check the USB root. It should contain the CSV and not contain WAV files or photo folders.
