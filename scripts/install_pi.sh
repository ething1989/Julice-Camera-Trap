#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/juara-wildlife-station}"
CONFIG_PATH="${CONFIG_PATH:-/etc/juara-station.toml}"
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.example.toml}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
TIMEZONE="${TIMEZONE:-America/Cuiaba}"
USB_LABEL="${USB_LABEL:-JULICE-CAM}"
USB_MOUNT="${USB_MOUNT:-/mnt/juara_usb}"
GPS_DEVICE="${GPS_DEVICE:-/dev/serial0}"
AUDIO_DEVICE="${AUDIO_DEVICE:-}"
RESET_CONFIG="${RESET_CONFIG:-1}"
INSTALL_BIRDNET="${INSTALL_BIRDNET:-1}"
BUILD_SPECIES_PACK="${BUILD_SPECIES_PACK:-1}"
GDRIVE_REMOTE="${GDRIVE_REMOTE:-juara-gdrive}"
GDRIVE_DIR="${GDRIVE_DIR:-Julice Camera Trap July 2026}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$APP_DIR/.venv/bin/python"

append_once() {
  local file="$1"
  local line="$2"
  grep -qxF "$line" "$file" || printf '\n%s\n' "$line" >> "$file"
}

set_key_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

set_toml_key_in_section() {
  local file="$1"
  local section="$2"
  local key="$3"
  local value="$4"
  python3 - "$file" "$section" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
section = sys.argv[2]
key = sys.argv[3]
value = sys.argv[4]
header = f"[{section}]"
lines = path.read_text().splitlines()
out = []
in_section = False
seen_section = False
written = False

for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_section and not written:
            out.append(f"{key} = {value}")
            written = True
        in_section = stripped == header
        seen_section = seen_section or in_section
    if in_section and (stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=")):
        if not written:
            out.append(f"{key} = {value}")
            written = True
        continue
    out.append(line)

if not seen_section:
    if out and out[-1] != "":
        out.append("")
    out.append(header)
    out.append(f"{key} = {value}")
elif in_section and not written:
    out.append(f"{key} = {value}")

path.write_text("\n".join(out) + "\n")
PY
}

install_extra() {
  local extra="$1"
  local label="$2"
  if "$VENV_PYTHON" -m pip install -e "$APP_DIR[$extra]"; then
    echo "Installed $label dependencies."
  else
    echo "WARNING: $label dependencies failed to install; the station will keep logging and retry that work."
  fi
}

make_module_writable() {
  local module="$1"
  local label="$2"
  local module_dir
  module_dir="$("$VENV_PYTHON" - "$module" <<'PY'
from pathlib import Path
import importlib.util
import sys

spec = importlib.util.find_spec(sys.argv[1])
if spec is None:
    raise SystemExit(1)
locations = spec.submodule_search_locations
if locations:
    print(locations[0])
elif spec.origin:
    print(Path(spec.origin).parent)
else:
    raise SystemExit(1)
PY
)" || {
    echo "WARNING: $label module is not importable yet; skipping writable package setup."
    return
  }
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$module_dir/checkpoints" 2>/dev/null || true
  chown -R "$SERVICE_USER:$SERVICE_USER" "$module_dir" 2>/dev/null || true
}

patch_birdnet_tflite_checker() {
  if "$VENV_PYTHON" - <<'PY'
from pathlib import Path
import birdnet_analyzer

utils_path = Path(birdnet_analyzer.__file__).parent / "utils.py"
text = utils_path.read_text()
start = text.index("def check_birdnet_files():")
end = text.index("\ndef ensure_model_exists", start)
replacement = '''def check_birdnet_files():
    checkpoint_dir = os.path.join(SCRIPT_DIR, "checkpoints", "V2.4")
    required_files = [
        "BirdNET_GLOBAL_6K_V2.4_Labels.txt",
        "BirdNET_GLOBAL_6K_V2.4_MData_Model_V2_FP16.tflite",
        "BirdNET_GLOBAL_6K_V2.4_Model_FP32.tflite",
    ]
    return all(os.path.exists(os.path.join(checkpoint_dir, file)) for file in required_files)
'''
if text[start:end] != replacement:
    backup = utils_path.with_suffix(".py.juara-backup")
    if not backup.exists():
        backup.write_text(text)
    utils_path.write_text(text[:start] + replacement + text[end:])
print(utils_path)
PY
  then
    echo "Patched BirdNET to accept a pre-staged TFLite model bundle."
  else
    echo "WARNING: BirdNET TFLite checker patch failed; analyzer may try to download the full model archive."
  fi
}

echo "Installing Julice station on $(uname -m) with $(python3 --version 2>&1)"
echo "Expected Wi-Fi from Raspberry Pi Imager: SSID JULICE, password EROS2016"

apt-get update
apt-get install -y \
  alsa-utils \
  ffmpeg \
  gpsd \
  gpsd-clients \
  i2c-tools \
  pigpio \
  python3-dev \
  python3-pigpio \
  python3-pip \
  python3-venv \
  python3-smbus \
  rclone \
  rsync \
  util-linux-extra

if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone "$TIMEZONE" || true
fi

systemctl disable --now apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service 2>/dev/null || true

if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
  raspi-config nonint do_serial_hw 0 || true
  raspi-config nonint do_serial_cons 1 || true
fi

append_once /boot/firmware/config.txt "dtparam=i2c_arm=on"
append_once /boot/firmware/config.txt "dtoverlay=i2c-rtc,ds3231"
append_once /boot/firmware/config.txt "enable_uart=1"
if [[ -f /boot/firmware/cmdline.txt ]]; then
  python3 - <<'PY'
from pathlib import Path

path = Path("/boot/firmware/cmdline.txt")
tokens = path.read_text().strip().split()
tokens = [token for token in tokens if token not in ("console=serial0,115200", "console=ttyS0,115200")]
path.write_text(" ".join(tokens) + "\n")
PY
fi
systemctl disable --now serial-getty@serial0.service serial-getty@ttyS0.service 2>/dev/null || true
systemctl mask serial-getty@serial0.service serial-getty@ttyS0.service 2>/dev/null || true
systemctl enable --now pigpiod.service 2>/dev/null || systemctl enable --now pigpiod 2>/dev/null || true
usermod -aG dialout gpsd 2>/dev/null || true

if [[ -f /etc/default/gpsd ]]; then
  set_key_value /etc/default/gpsd DEVICES "\"$GPS_DEVICE\""
  set_key_value /etc/default/gpsd GPSD_OPTIONS "\"-n\""
  set_key_value /etc/default/gpsd USBAUTO "\"true\""
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR"
rsync -a --delete \
  --exclude ".DS_Store" \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".local-tests" \
  --exclude ".local_run" \
  --exclude ".pytest_cache" \
  --exclude "__pycache__" \
  --exclude "*.egg-info" \
  --exclude "data/bird_playback_test" \
  "$REPO_DIR/" "$APP_DIR/"

python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$APP_DIR"
install_extra pi "Pi hardware"

if [[ "$INSTALL_BIRDNET" == "1" ]]; then
  install_extra birdnet "BirdNET audio AI"
  make_module_writable birdnet_analyzer "BirdNET audio AI"
  patch_birdnet_tflite_checker
  if [[ "$BUILD_SPECIES_PACK" == "1" && ! -d "$APP_DIR/data/BirdNET_100mi_PrimaryPlus/cells" ]]; then
    echo "Building 100-mile BirdNET species pack. This can take a while on a Pi Zero 2 W."
    runuser -u "$SERVICE_USER" -- "$VENV_PYTHON" "$APP_DIR/scripts/build_birdnet_100mi_species_pack.py" \
      --output "$APP_DIR/data/BirdNET_100mi_PrimaryPlus" \
      --threads 1 || {
        echo "WARNING: Could not build the 100-mile BirdNET species pack; generated species filtering will retry after setup."
      }
  fi
fi

if [[ "$RESET_CONFIG" == "1" || ! -f "$CONFIG_PATH" ]]; then
  install -m 0644 "$APP_DIR/$CONFIG_TEMPLATE" "$CONFIG_PATH"
fi
if [[ -n "$AUDIO_DEVICE" ]]; then
  set_toml_key_in_section "$CONFIG_PATH" audio device "\"$AUDIO_DEVICE\""
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/juara-station/state
if [[ -f "$APP_DIR/data/birdnet/julice_active_species_list.txt" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
    "$APP_DIR/data/birdnet/julice_active_species_list.txt" \
    /var/lib/juara-station/state/juara-birdnet-species-list.txt
fi
if [[ "$INSTALL_BIRDNET" == "1" && -d "$APP_DIR/data/BirdNET_100mi_PrimaryPlus/cells" ]]; then
  runuser -u "$SERVICE_USER" -- "$VENV_PYTHON" -m juara_station.cli --config "$CONFIG_PATH" select-species || {
    echo "WARNING: Dynamic BirdNET species-pack selection failed; station will keep retrying when GPS/fallback coordinates are available."
  }
fi

usermod -aG audio,i2c,gpio,plugdev,dialout "$SERVICE_USER" || true
sudoers_file="/etc/sudoers.d/juara-station-hwclock"
printf '%s ALL=(root) NOPASSWD: /usr/sbin/hwclock *\n' "$SERVICE_USER" > "$sudoers_file"
chmod 0440 "$sudoers_file"
visudo -cf "$sudoers_file" >/dev/null
reboot_sudoers_file="/etc/sudoers.d/juara-station-reboot"
printf '%s ALL=(root) NOPASSWD: /usr/sbin/reboot\n' "$SERVICE_USER" > "$reboot_sudoers_file"
chmod 0440 "$reboot_sudoers_file"
visudo -cf "$reboot_sudoers_file" >/dev/null
systemctl_sudoers_file="/etc/sudoers.d/juara-station-systemctl"
{
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl stop juara-ai-worker.service\n' "$SERVICE_USER"
  printf '%s ALL=(root) NOPASSWD: /bin/systemctl stop juara-ai-worker.service\n' "$SERVICE_USER"
} > "$systemctl_sudoers_file"
chmod 0440 "$systemctl_sudoers_file"
visudo -cf "$systemctl_sudoers_file" >/dev/null

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$USB_MOUNT" /var/lib/juara-station
usb_device="$(blkid -L "$USB_LABEL" 2>/dev/null || true)"
if [[ -z "$usb_device" ]]; then
  usb_device="$(lsblk -rpno NAME,TYPE,FSTYPE | awk '$1 ~ "^/dev/sd" && $2 == "part" && $3 != "" { print $1; exit }')"
fi
if [[ -n "$usb_device" ]]; then
  usb_uuid="$(blkid -s UUID -o value "$usb_device")"
  usb_fstype="$(blkid -s TYPE -o value "$usb_device")"
  if [[ -n "$usb_uuid" && -n "$usb_fstype" ]]; then
    user_uid="$(id -u "$SERVICE_USER")"
    user_gid="$(id -g "$SERVICE_USER")"
    tmp_fstab="$(mktemp)"
    awk -v mountpoint="$USB_MOUNT" '$2 != mountpoint { print }' /etc/fstab > "$tmp_fstab"
    cat "$tmp_fstab" > /etc/fstab
    rm -f "$tmp_fstab"
    if [[ "$usb_fstype" == "vfat" || "$usb_fstype" == "exfat" ]]; then
      mount_options="defaults,nofail,x-systemd.automount,uid=$user_uid,gid=$user_gid,umask=0022"
    else
      mount_options="defaults,nofail,x-systemd.automount"
    fi
    printf 'UUID=%s %s %s %s 0 0\n' "$usb_uuid" "$USB_MOUNT" "$usb_fstype" "$mount_options" >> /etc/fstab
    systemctl daemon-reload
    mount "$USB_MOUNT" || true
  else
    echo "WARNING: USB partition $usb_device has no UUID or filesystem type; station will use fallback storage until USB is mounted."
  fi
else
  user_uid="$(id -u "$SERVICE_USER")"
  user_gid="$(id -g "$SERVICE_USER")"
  tmp_fstab="$(mktemp)"
  awk -v mountpoint="$USB_MOUNT" '$2 != mountpoint { print }' /etc/fstab > "$tmp_fstab"
  cat "$tmp_fstab" > /etc/fstab
  rm -f "$tmp_fstab"
  printf 'LABEL=%s %s auto defaults,nofail,x-systemd.automount,x-systemd.device-timeout=10s,uid=%s,gid=%s,umask=0022 0 0\n' \
    "$USB_LABEL" "$USB_MOUNT" "$user_uid" "$user_gid" >> /etc/fstab
  systemctl daemon-reload
  echo "WARNING: USB drive label $USB_LABEL not found and no USB partition was detected; installed a label-based automount entry and station will use fallback storage until USB is mounted."
fi

mkdir -p \
  /var/lib/juara-station/state \
  /var/lib/juara-station/audio_recordings \
  /tmp/juara-ai-work \
  /tmp/juara-audio
rm -rf "$USB_MOUNT/audio" "$USB_MOUNT/media/audio" "$USB_MOUNT/Photos" "$USB_MOUNT/media/photos" "$USB_MOUNT/juara/photos" 2>/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$USB_MOUNT" /var/lib/juara-station /tmp/juara-ai-work /tmp/juara-audio 2>/dev/null || true

cat > /usr/local/bin/juara-planned-reboot <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$APP_DIR"
CONFIG_PATH="$CONFIG_PATH"
VENV_PYTHON="\$APP_DIR/.venv/bin/python"
TIMEOUT_SECONDS="\${JUARA_PLANNED_REBOOT_TIMEOUT_SECONDS:-180}"
export APP_DIR CONFIG_PATH VENV_PYTHON

cleanup() {
  systemctl stop juara-station.service juara-ai-worker.service 2>/dev/null || true
  "\$VENV_PYTHON" -m juara_station.cli --config "\$CONFIG_PATH" planned-reboot-cleanup || true
}

if ! timeout --kill-after=15s "\${TIMEOUT_SECONDS}s" bash -c "\$(declare -f cleanup); cleanup"; then
  echo "WARNING: Juara planned reboot cleanup timed out after \${TIMEOUT_SECONDS}s; rebooting anyway." >&2
fi

systemctl reboot
EOF
chmod 0755 /usr/local/bin/juara-planned-reboot

install -m 0755 "$APP_DIR/scripts/juara_gdrive_sync" /usr/local/bin/juara_gdrive_sync
install -m 0755 "$APP_DIR/scripts/juara_gdrive_auth_helper" /usr/local/bin/juara_gdrive_auth_helper
install -m 0755 "$APP_DIR/scripts/juara_git_update" /usr/local/bin/juara_git_update
install -m 0755 "$APP_DIR/scripts/juara_networkpi_maintenance" /usr/local/bin/juara_networkpi_maintenance
install -m 0755 "$APP_DIR/scripts/juara_wifi_watchdog" /usr/local/bin/juara_wifi_watchdog
cat > /etc/default/juara-gdrive-sync <<EOF
JUARA_LOCAL_ROOT=/var/lib/juara-station/local
JUARA_GDRIVE_REMOTE="$GDRIVE_REMOTE"
JUARA_GDRIVE_DIR="$GDRIVE_DIR"
JUARA_GDRIVE_LOG=/var/log/juara-gdrive-sync.log
JUARA_GDRIVE_TRANSFERS=1
JUARA_GDRIVE_CHECKERS=2
JUARA_GDRIVE_RETRIES=8
JUARA_GDRIVE_LOW_LEVEL_RETRIES=30
JUARA_GDRIVE_RETRIES_SLEEP=2m
JUARA_GDRIVE_CONNECT_TIMEOUT=60s
JUARA_GDRIVE_IO_TIMEOUT=10m
EOF
cat > /etc/default/juara-git-update <<EOF
JUARA_SERVICE_USER=$SERVICE_USER
JUARA_GIT_REPO_URL=https://github.com/ething1989/Julice-Camera-Trap.git
JUARA_GIT_BRANCH=main
JUARA_GIT_CHECKOUT_DIR=/var/lib/juara-station/git/Julice-Camera-Trap
JUARA_DEPLOY_DIR=$APP_DIR
EOF
cat > /etc/default/juara-networkpi-maintenance <<EOF
JUARA_SERVICE_USER=$SERVICE_USER
JUARA_NETWORKPI_SSID=NetworkPi
JUARA_GDRIVE_ENV=/etc/default/juara-gdrive-sync
JUARA_SERVICE_REBOOT_TIMER=juara-service-hourly-reboot.timer
EOF
touch /var/log/juara-gdrive-sync.log
chown "$SERVICE_USER:$SERVICE_USER" /var/log/juara-gdrive-sync.log

sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-station.service.in" > /etc/systemd/system/juara-station.service
sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-ai-worker.service.in" > /etc/systemd/system/juara-ai-worker.service
sed "s#__USER__#$SERVICE_USER#g" \
  "$APP_DIR/systemd/juara-gdrive-sync.service.in" > /etc/systemd/system/juara-gdrive-sync.service
install -m 0644 "$APP_DIR/systemd/juara-gdrive-sync.timer" /etc/systemd/system/juara-gdrive-sync.timer
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.service" /etc/systemd/system/juara-daily-reboot.service
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.timer" /etc/systemd/system/juara-daily-reboot.timer
install -m 0644 "$APP_DIR/systemd/juara-networkpi-maintenance.service" /etc/systemd/system/juara-networkpi-maintenance.service
install -m 0644 "$APP_DIR/systemd/juara-networkpi-maintenance.timer" /etc/systemd/system/juara-networkpi-maintenance.timer
install -m 0644 "$APP_DIR/systemd/juara-git-update.service" /etc/systemd/system/juara-git-update.service
install -m 0644 "$APP_DIR/systemd/juara-git-update.timer" /etc/systemd/system/juara-git-update.timer
install -m 0644 "$APP_DIR/systemd/juara-service-hourly-reboot.service" /etc/systemd/system/juara-service-hourly-reboot.service
install -m 0644 "$APP_DIR/systemd/juara-service-hourly-reboot.timer" /etc/systemd/system/juara-service-hourly-reboot.timer
install -m 0644 "$APP_DIR/scripts/systemd/juara-wifi-watchdog.service" /etc/systemd/system/juara-wifi-watchdog.service
install -m 0644 "$APP_DIR/scripts/systemd/juara-wifi-watchdog.timer" /etc/systemd/system/juara-wifi-watchdog.timer

systemctl daemon-reload
systemctl enable gpsd.socket gpsd.service || true
systemctl restart gpsd.socket gpsd.service || true
systemctl enable juara-station.service
systemctl enable juara-ai-worker.service
systemctl enable --now juara-gdrive-sync.timer
systemctl enable --now juara-daily-reboot.timer
systemctl enable --now juara-networkpi-maintenance.timer
systemctl enable --now juara-git-update.timer
systemctl enable --now juara-wifi-watchdog.timer
systemctl disable --now juara-service-hourly-reboot.timer || true

"$VENV_PYTHON" - <<'PY' || true
import importlib.util
for module in ("juara_station", "birdnet_analyzer", "board", "busio", "adafruit_bme280", "adafruit_veml7700", "pigpio"):
    print(f"{module}={bool(importlib.util.find_spec(module))}")
PY

cat <<EOF

Installed Julice station for user $SERVICE_USER
Config: $CONFIG_PATH
USB mount: $USB_MOUNT
CSV: $USB_MOUNT/Julice Camera Trap July 2026.csv
Google Drive folder: $GDRIVE_REMOTE:$GDRIVE_DIR

Google Drive still needs a human login once per Pi:
  sudo -u "$SERVICE_USER" /usr/local/bin/juara_gdrive_auth_helper
  sudo -u "$SERVICE_USER" rclone config reconnect "$GDRIVE_REMOTE":
  sudo -u "$SERVICE_USER" /usr/local/bin/juara_gdrive_sync

Service controls:
  sudo systemctl start juara-station
  sudo systemctl start juara-ai-worker
  sudo journalctl -u juara-station -f
EOF
