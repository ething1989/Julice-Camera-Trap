from juara_station.config import SensorConfig
from juara_station.sensors import SensorSuite


class BrokenBme:
    @property
    def temperature(self):
        raise RuntimeError("i2c glitch")


class GoodBme:
    temperature = 26.0
    relative_humidity = 75.0
    pressure = 1000.0


class GoodLux:
    lux = 123.0


class GoodScd41:
    data_ready = True
    CO2 = 456


def test_sensor_read_failure_marks_device_for_later_reinit():
    suite = SensorSuite(SensorConfig(enabled=False))
    suite.config = SensorConfig(enabled=True)
    suite._bme = BrokenBme()
    suite._veml = GoodLux()
    suite._scd4x = None

    sample = suite.sample()

    assert sample.temperature_c is None
    assert sample.lux == 123.0
    assert suite._bme is None
    assert any("BME280 read failed" in error for error in suite.init_errors)


def test_missing_sensor_reinitializes_after_retry_window(monkeypatch):
    suite = SensorSuite(SensorConfig(enabled=False))
    suite.config = SensorConfig(enabled=True)
    suite._retry_init_seconds = 60.0
    suite._last_init_attempt_monotonic = 100.0

    times = iter([120.0, 161.0])
    monkeypatch.setattr("juara_station.sensors.time.monotonic", lambda: next(times))

    def fake_init():
        suite._bme = GoodBme()
        suite._veml = GoodLux()

    monkeypatch.setattr(suite, "_init_hardware", fake_init)

    first = suite.sample()
    second = suite.sample()

    assert first.temperature_c is None
    assert second.temperature_c == 26.0
    assert second.lux == 123.0


def test_scd41_sample_is_optional():
    suite = SensorSuite(SensorConfig(enabled=False))
    suite.config = SensorConfig(enabled=True, scd41_enabled=True)
    suite._bme = GoodBme()
    suite._veml = GoodLux()
    suite._scd4x = GoodScd41()

    sample = suite.sample()

    assert sample.co2_ppm == 456.0
