#!/usr/bin/env python3
from __future__ import annotations

from juara_station.config import load_config
from juara_station.csv_exporter import MMHG_PER_INHG
from juara_station.sensors import SensorSuite


def main() -> int:
    config = load_config("/etc/juara-station.toml")
    print(f"configured_veml7700_address=0x{config.sensors.veml7700_address:02x}")
    try:
        import board
        import busio

        i2c = busio.I2C(board.SCL, board.SDA)
        while not i2c.try_lock():
            pass
        try:
            addresses = i2c.scan()
        finally:
            i2c.unlock()
        print("i2c_addresses=" + ",".join(f"0x{address:02x}" for address in addresses))
        print(f"veml7700_present={config.sensors.veml7700_address in addresses}")
    except Exception as exc:
        print(f"i2c_scan_error={exc}")

    suite = SensorSuite(config.sensors)
    print("sensor_init_errors=" + " | ".join(suite.init_errors))
    sample = suite.sample()
    print(f"sample_lux={sample.lux}")
    print(f"sample_temperature_c={sample.temperature_c}")
    print(f"sample_humidity_pct={sample.humidity_pct}")
    sample_pressure_inhg = sample.pressure_mmhg / MMHG_PER_INHG if sample.pressure_mmhg is not None else None
    print(f"sample_pressure_inhg={sample_pressure_inhg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
