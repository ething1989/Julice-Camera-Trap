#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo from the repo root:"
  echo "  sudo scripts/install_julice_camera_trap.sh"
  exit 1
fi

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-pi}}"

cd "$REPO_DIR"
SERVICE_USER="$SERVICE_USER" \
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.example.toml}" \
RESET_CONFIG="${RESET_CONFIG:-1}" \
INSTALL_BIRDNET="${INSTALL_BIRDNET:-1}" \
BUILD_SPECIES_PACK="${BUILD_SPECIES_PACK:-1}" \
scripts/install_pi.sh
