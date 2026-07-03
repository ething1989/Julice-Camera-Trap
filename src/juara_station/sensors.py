from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import random
import time

from .config import SensorConfig
from .storage import SensorSample, utc_now


HPA_TO_MMHG = 0.750061683
MHZ19_READ_CO2_COMMAND = bytes([0xFF, 0x01, 0x86, 0x00, 0x00, 0x00, 0x00, 0x00, 0x79])


def _mhz19_checksum(frame: bytes | bytearray) -> int:
    return (0xFF - (sum(frame[1:8]) & 0xFF) + 1) & 0xFF


class SoftUartMhz19Co2:
    """Read an MH-Z19-style UART CO2 sensor through pigpio bit-banged serial."""

    def __init__(self, rx_gpio: int, tx_gpio: int, baudrate: int = 9600) -> None:
        import pigpio

        self.rx_gpio = rx_gpio
        self.tx_gpio = tx_gpio
        self.baudrate = baudrate
        self._pigpio = pigpio
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod is not running")
        self._pi.set_mode(self.tx_gpio, pigpio.OUTPUT)
        self._pi.write(self.tx_gpio, 1)
        try:
            self._pi.bb_serial_read_close(self.rx_gpio)
        except pigpio.error:
            pass
        self._pi.bb_serial_read_open(self.rx_gpio, self.baudrate, 8)

    def close(self) -> None:
        try:
            self._pi.bb_serial_read_close(self.rx_gpio)
        except Exception:
            pass
        try:
            self._pi.stop()
        except Exception:
            pass

    def read_ppm(self, timeout_seconds: float = 1.5) -> int:
        try:
            self._pi.bb_serial_read(self.rx_gpio)
        except self._pigpio.error:
            pass
        self._send(MHZ19_READ_CO2_COMMAND)
        deadline = time.monotonic() + timeout_seconds
        buffer = bytearray()
        while time.monotonic() < deadline:
            count, data = self._pi.bb_serial_read(self.rx_gpio)
            if count and data:
                buffer.extend(data)
                frame = self._find_response_frame(buffer)
                if frame is not None:
                    return frame[2] * 256 + frame[3]
            time.sleep(0.02)
        raise TimeoutError(f"no CO2 UART response on GPIO{self.rx_gpio}")

    def _send(self, data: bytes) -> None:
        self._pi.wave_clear()
        try:
            self._pi.wave_add_serial(self.tx_gpio, self.baudrate, data)
        except TypeError:
            self._pi.wave_add_serial(self.tx_gpio, self.baudrate, data.decode("latin1"))
        wave_id = self._pi.wave_create()
        if wave_id < 0:
            raise RuntimeError(f"pigpio wave_create failed: {wave_id}")
        try:
            self._pi.wave_send_once(wave_id)
            deadline = time.monotonic() + 1.0
            while self._pi.wave_tx_busy() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            self._pi.wave_delete(wave_id)

    def _find_response_frame(self, buffer: bytearray) -> bytes | None:
        for index in range(max(0, len(buffer) - 8)):
            frame = bytes(buffer[index : index + 9])
            if frame[0] == 0xFF and frame[1] == 0x86 and _mhz19_checksum(frame) == frame[8]:
                return frame
        if len(buffer) > 32:
            del buffer[:-16]
        return None


@dataclass
class SensorReadings:
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_mmhg: float | None = None
    lux: float | None = None
    co2_ppm: float | None = None
    pm1_0_ug_m3: float | None = None
    pm2_5_ug_m3: float | None = None
    pm10_ug_m3: float | None = None
    particles_0_3_per_l: float | None = None
    particles_0_5_per_l: float | None = None
    cpu_temp_c: float | None = None


class SensorSuite:
    def __init__(self, config: SensorConfig):
        self.config = config
        self._bme = None
        self._veml = None
        self._scd4x = None
        self._uart_co2 = None
        self._uart_co2_warm_until = 0.0
        self._pms = None
        self._pms_read_timeout_error = None
        self._pms_warm_until = 0.0
        self._init_errors: list[str] = []
        self._max_init_errors = 50
        self._last_init_attempt_monotonic = 0.0
        self._retry_init_seconds = 60.0
        if config.enabled:
            self._last_init_attempt_monotonic = time.monotonic()
            self._init_hardware()

    @property
    def init_errors(self) -> list[str]:
        return list(self._init_errors)

    def _remember_error(self, message: str) -> None:
        if self._init_errors and self._init_errors[-1] == message:
            return
        self._init_errors.append(message)
        if len(self._init_errors) > self._max_init_errors:
            del self._init_errors[: len(self._init_errors) - self._max_init_errors]

    def sample(self) -> SensorSample:
        self._maybe_reinit_hardware()
        readings = SensorReadings()
        errors: list[str] = []
        stagger_seconds = max(0.0, float(self.config.stagger_read_seconds))
        if self._bme is not None:
            try:
                readings.temperature_c = float(self._bme.temperature)
                readings.humidity_pct = float(self._bme.relative_humidity)
                readings.pressure_mmhg = float(self._bme.pressure) * HPA_TO_MMHG
            except Exception as exc:  # Hardware libraries raise broad OSErrors/RuntimeErrors.
                self._remember_error(f"BME280 read failed: {exc}")
                errors.append("BME Connection")
                self._bme = None
        elif self.config.enabled:
            errors.append("BME Connection")
        if stagger_seconds:
            time.sleep(stagger_seconds)
        if self._veml is not None:
            try:
                readings.lux = float(self._veml.lux)
            except Exception as exc:
                self._remember_error(f"VEML7700 read failed: {exc}")
                errors.append("LUX Connection")
                self._veml = None
        elif self.config.enabled:
            errors.append("LUX Connection")
        if stagger_seconds:
            time.sleep(stagger_seconds)
        readings.cpu_temp_c = read_cpu_temp()
        if self._scd4x is not None:
            try:
                if getattr(self._scd4x, "data_ready", True):
                    readings.co2_ppm = float(self._scd4x.CO2)
            except Exception as exc:
                self._remember_error(f"SCD41 read failed: {exc}")
                self._scd4x = None
        if self._uart_co2 is not None and readings.co2_ppm is None and time.monotonic() >= self._uart_co2_warm_until:
            try:
                readings.co2_ppm = float(self._uart_co2.read_ppm())
            except Exception as exc:
                self._remember_error(f"UART CO2 read failed: {exc}")
                try:
                    self._uart_co2.close()
                except Exception:
                    pass
                self._uart_co2 = None
        if self._pms is not None and time.monotonic() >= self._pms_warm_until:
            try:
                data = self._pms.read()
                readings.pm1_0_ug_m3 = float(data.pm_ug_per_m3(1.0))
                readings.pm2_5_ug_m3 = float(data.pm_ug_per_m3(2.5))
                readings.pm10_ug_m3 = float(data.pm_ug_per_m3(10))
                readings.particles_0_3_per_l = float(data.pm_per_1l_air(0.3))
                readings.particles_0_5_per_l = float(data.pm_per_1l_air(0.5))
            except Exception as exc:
                if self._pms_read_timeout_error is not None and isinstance(exc, self._pms_read_timeout_error):
                    self._remember_error(f"PMS5003 read timed out on {self.config.pms5003_device}")
                else:
                    self._remember_error(f"PMS5003 read failed: {exc}")
                    self._pms = None
        unique_errors = tuple(dict.fromkeys(errors))
        return SensorSample(
            sampled_at=utc_now(),
            temperature_c=readings.temperature_c,
            humidity_pct=readings.humidity_pct,
            pressure_mmhg=readings.pressure_mmhg,
            lux=readings.lux,
            co2_ppm=readings.co2_ppm,
            pm1_0_ug_m3=readings.pm1_0_ug_m3,
            pm2_5_ug_m3=readings.pm2_5_ug_m3,
            pm10_ug_m3=readings.pm10_ug_m3,
            particles_0_3_per_l=readings.particles_0_3_per_l,
            particles_0_5_per_l=readings.particles_0_5_per_l,
            cpu_temp_c=readings.cpu_temp_c,
            errors=unique_errors,
        )

    def _init_hardware(self) -> None:
        try:
            import board
            import busio
            from adafruit_bme280 import basic as adafruit_bme280
            import adafruit_veml7700

            i2c = busio.I2C(board.SCL, board.SDA)
            try:
                self._bme = self._init_bme280(adafruit_bme280, i2c)
            except Exception as exc:
                self._remember_error(f"BME280 init failed: {exc}")
                try:
                    import adafruit_bme680

                    self._bme = adafruit_bme680.Adafruit_BME680_I2C(i2c, address=self.config.bme280_address)
                    self._remember_error("BME680 fallback active")
                except Exception as fallback_exc:
                    self._remember_error(f"BME680 init failed: {fallback_exc}")
            try:
                self._veml = adafruit_veml7700.VEML7700(i2c, address=self.config.veml7700_address)
            except Exception as exc:
                self._remember_error(f"VEML7700 init failed: {exc}")
            if self.config.scd41_enabled:
                try:
                    import adafruit_scd4x

                    try:
                        self._scd4x = adafruit_scd4x.SCD4X(i2c, address=self.config.scd41_address)
                    except TypeError:
                        self._scd4x = adafruit_scd4x.SCD4X(i2c)
                    self._scd4x.start_periodic_measurement()
                except Exception as exc:
                    self._remember_error(f"SCD41 init failed: {exc}")
        except Exception as exc:
            self._remember_error(f"I2C sensor stack unavailable: {exc}")
        if self.config.uart_co2_enabled and self._uart_co2 is None:
            try:
                self._uart_co2 = SoftUartMhz19Co2(
                    rx_gpio=self.config.uart_co2_rx_gpio,
                    tx_gpio=self.config.uart_co2_tx_gpio,
                    baudrate=self.config.uart_co2_baudrate,
                )
                self._uart_co2_warm_until = time.monotonic() + max(0, self.config.uart_co2_warmup_seconds)
            except Exception as exc:
                self._remember_error(f"UART CO2 init failed: {exc}")
        if self.config.pms5003_enabled and self._pms is None:
            try:
                from pms5003 import PMS5003, ReadTimeoutError

                self._pms = PMS5003(device=self.config.pms5003_device, baudrate=self.config.pms5003_baudrate)
                self._pms_read_timeout_error = ReadTimeoutError
                self._pms_warm_until = time.monotonic() + max(0, self.config.pms5003_warmup_seconds)
            except Exception as exc:
                self._remember_error(f"PMS5003 init failed: {exc}")

    def _maybe_reinit_hardware(self) -> None:
        if not self.config.enabled:
            return
        if (
            self._bme is not None
            and self._veml is not None
            and (not self.config.scd41_enabled or self._scd4x is not None)
            and (not self.config.uart_co2_enabled or self._uart_co2 is not None)
            and (not self.config.pms5003_enabled or self._pms is not None)
        ):
            return
        now = time.monotonic()
        if now - self._last_init_attempt_monotonic < self._retry_init_seconds:
            return
        self._last_init_attempt_monotonic = now
        self._init_hardware()

    def _init_bme280(self, adafruit_bme280, i2c):
        addresses = [self.config.bme280_address]
        for address in (0x76, 0x77):
            if address not in addresses:
                addresses.append(address)
        last_exc: Exception | None = None
        for address in addresses:
            try:
                sensor = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=address)
                if address != self.config.bme280_address:
                    self._init_errors.append(f"BME280 fallback address 0x{address:02x} active")
                return sensor
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc


class MockSensorSuite(SensorSuite):
    def __init__(self) -> None:
        self.config = SensorConfig(enabled=False)
        self._bme = None
        self._veml = None
        self._scd4x = None
        self._uart_co2 = None
        self._uart_co2_warm_until = 0.0
        self._pms = None
        self._pms_read_timeout_error = None
        self._pms_warm_until = 0.0
        self._init_errors = []
        self._max_init_errors = 50
        self._last_init_attempt_monotonic = 0.0
        self._retry_init_seconds = 60.0

    def sample(self) -> SensorSample:
        return SensorSample(
            sampled_at=utc_now(),
            temperature_c=25.0 + random.random(),
            humidity_pct=70.0 + random.random() * 5,
            pressure_mmhg=755.0 + random.random() * 2,
            lux=12000.0 + random.random() * 500,
            co2_ppm=420.0 + random.random() * 20,
            pm1_0_ug_m3=4.0 + random.random(),
            pm2_5_ug_m3=8.0 + random.random(),
            pm10_ug_m3=12.0 + random.random(),
            particles_0_3_per_l=1200.0 + random.random() * 20,
            particles_0_5_per_l=700.0 + random.random() * 20,
            cpu_temp_c=42.0 + random.random(),
        )


def read_cpu_temp() -> float | None:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(path.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None
