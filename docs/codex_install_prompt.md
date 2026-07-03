# Prompt For Another Codex

You are setting up the `Julice-Camera-Trap` Raspberry Pi station. The target system has GPS, RTC, BME280, VEML7700, USB thumb drive, and USB microphone. It has no camera hardware, no motion detector, no flash, no image AI, and should not create photo folders or photo CSV fields.

Use this repository:

```bash
git clone https://github.com/esmaby444/Julice-Camera-Trap.git ~/Julice-Camera-Trap
cd ~/Julice-Camera-Trap
sudo scripts/install_julice_camera_trap.sh
```

The SD card should already connect to Wi-Fi:

```text
SSID: JULICE
Password: EROS2016
```

After install, complete Google Drive login with the human present:

```bash
sudo -u "$USER" /usr/local/bin/juara_gdrive_auth_helper
sudo -u "$USER" rclone config reconnect juara-gdrive:
sudo -u "$USER" /usr/local/bin/juara_gdrive_sync
```

Expected Drive folder:

```text
Julice Camera Trap July 2026
```

Run checks:

```bash
sudo scripts/pi_preflight.sh
sudo systemctl restart juara-station juara-ai-worker
sleep 20
sudo journalctl -u juara-station -n 120 --no-pager
sudo journalctl -u juara-ai-worker -n 120 --no-pager
ls -lh /mnt/juara_usb
```

Confirm these before deployment:

- `/mnt/juara_usb/Julice Camera Trap July 2026.csv` exists after a field interval or manual export.
- No audio folder exists on the USB.
- No photo folder exists on the USB.
- `/etc/juara-station.toml` has no camera or speciesnet sections.
- `systemctl is-enabled juara-station juara-ai-worker juara-gdrive-sync.timer juara-daily-reboot.timer` reports enabled.
- `rclone listremotes` includes `juara-gdrive:`.
- If internet is disconnected, station logging still continues and Drive sync exits without breaking the service.
