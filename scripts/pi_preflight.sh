#!/usr/bin/env bash
set -u

CONFIG_PATH="${CONFIG_PATH:-/etc/juara-station.toml}"
APP_DIR="${APP_DIR:-/opt/juara-wildlife-station}"
USB_MOUNT="${USB_MOUNT:-/mnt/juara_usb}"
RUN_ACTIVE_TESTS="${RUN_ACTIVE_TESTS:-1}"

section() {
  printf '\n== %s ==\n' "$1"
}

run() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1 || printf 'COMMAND_FAILED exit=%s\n' "$?"
}

run_shell() {
  printf '\n$ %s\n' "$1"
  bash -lc "$1" 2>&1 || printf 'COMMAND_FAILED exit=%s\n' "$?"
}

section "System"
run hostname
run uname -a
run_shell "cat /etc/os-release | sed -n '1,8p'"
run python3 --version
run timedatectl

section "Storage"
run lsblk -o NAME,LABEL,UUID,FSTYPE,SIZE,MOUNTPOINTS
run findmnt "$USB_MOUNT"
run_shell "test -w '$USB_MOUNT' && echo usb_root_writable=yes || echo usb_root_writable=no"
run_shell "test ! -d '$USB_MOUNT/media/audio' && test ! -d '$USB_MOUNT/audio' && echo usb_audio_folders_absent=yes || echo usb_audio_folders_absent=no"
run df -h "$USB_MOUNT" /

section "Boot Config"
run_shell "grep -nE '^(dtparam=i2c_arm|dtoverlay=i2c-rtc|enable_uart)' /boot/firmware/config.txt || true"

section "Devices"
run_shell "ls -l /dev/i2c* /dev/rtc* /dev/serial* /dev/ttyAMA* /dev/ttyS* 2>/dev/null || true"
run groups

section "I2C"
if [[ -e /dev/i2c-1 ]]; then
  if command -v i2cdetect >/dev/null 2>&1; then
    run i2cdetect -y 1
  elif [[ -x /usr/sbin/i2cdetect ]]; then
    run /usr/sbin/i2cdetect -y 1
  else
    echo "i2cdetect is not installed or not on PATH."
  fi
else
  echo "/dev/i2c-1 is not present."
fi

section "RTC"
if command -v hwclock >/dev/null 2>&1; then
  if ! sudo hwclock --show --utc 2>/tmp/juara_hwclock_error.txt; then
    cat /tmp/juara_hwclock_error.txt
    run sudo hwclock -r -u
  fi
else
  echo "hwclock is not installed."
fi

section "GPS"
run_shell "sed -n '1,80p' /etc/default/gpsd 2>/dev/null || true"
if command -v gpspipe >/dev/null 2>&1; then
  run timeout 10 gpspipe -w -n 10
else
  echo "gpspipe is not installed."
fi

section "Audio"
if command -v arecord >/dev/null 2>&1; then
  run arecord -l
  if [[ "$RUN_ACTIVE_TESTS" == "1" ]]; then
    audio_device="default"
    if [[ -f "$CONFIG_PATH" ]]; then
      audio_device="$(awk '
        $0 ~ /^\[audio\]/ { in_audio=1; next }
        $0 ~ /^\[/ { in_audio=0 }
        in_audio && $1 == "device" {
          gsub(/"/, "", $3)
          print $3
          exit
        }
      ' "$CONFIG_PATH")"
      audio_device="${audio_device:-default}"
    fi
    run arecord -D "$audio_device" -f S16_LE -r 24000 -c 1 -d 2 -t wav /tmp/juara_audio_test.wav
    run ls -lh /tmp/juara_audio_test.wav
  fi
else
  echo "arecord is not installed."
fi

section "Google Drive"
if command -v rclone >/dev/null 2>&1; then
  run rclone listremotes
  run_shell "systemctl --no-pager --full status juara-gdrive-sync.timer 2>&1 | sed -n '1,80p' || true"
else
  echo "rclone is not installed."
fi

section "Station Python"
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  run "$APP_DIR/.venv/bin/python" --version
  run_shell "'$APP_DIR/.venv/bin/python' - <<'PY'
import importlib.util
for module in ('juara_station', 'birdnet_analyzer', 'board', 'busio', 'adafruit_bme280', 'adafruit_veml7700', 'pigpio'):
    print(f'{module}={bool(importlib.util.find_spec(module))}')
PY"
else
  echo "$APP_DIR/.venv/bin/python is not present yet."
fi

section "Station Service"
run_shell "systemctl is-enabled juara-station.service juara-ai-worker.service juara-gdrive-sync.timer juara-daily-reboot.timer gpsd.service gpsd.socket 2>&1 || true"
run_shell "systemctl --no-pager --full status juara-station.service 2>&1 | sed -n '1,80p' || true"
run_shell "systemctl --no-pager --full status juara-ai-worker.service 2>&1 | sed -n '1,80p' || true"
if [[ -f "$CONFIG_PATH" ]]; then
  run_shell "sed -n '1,220p' '$CONFIG_PATH'"
else
  echo "$CONFIG_PATH is not present yet."
fi
