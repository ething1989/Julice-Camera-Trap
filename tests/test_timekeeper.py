from datetime import datetime, timezone

from juara_station.config import TimeConfig
from juara_station.storage import DataStore
from juara_station.timekeeper import TimeKeeper


class FakeTimeKeeper(TimeKeeper):
    def __init__(self, config, store, gps, rtc):
        super().__init__(config, store)
        self.gps = gps
        self.rtc = rtc
        self.writes = []

    def _read_gps_time(self):
        return self.gps

    def _read_rtc_time(self):
        return self.rtc

    def _write_rtc_time(self, timestamp):
        self.writes.append(timestamp)


def test_uses_rtc_when_gps_is_very_different(tmp_path):
    store = DataStore(tmp_path / "state.sqlite3")
    gps = datetime(2026, 5, 10, 12, 10, tzinfo=timezone.utc)
    rtc = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    keeper = FakeTimeKeeper(TimeConfig(), store, gps, rtc)

    reading = keeper.now()

    assert reading.timestamp == rtc
    assert reading.source == "rtc"
    assert keeper.writes == []


def test_resyncs_rtc_after_three_large_gps_drifts(tmp_path):
    store = DataStore(tmp_path / "state.sqlite3")
    gps = datetime(2026, 5, 10, 12, 10, tzinfo=timezone.utc)
    rtc = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    keeper = FakeTimeKeeper(TimeConfig(), store, gps, rtc)

    keeper.now()
    keeper.now()
    reading = keeper.now()

    assert reading.timestamp == gps
    assert reading.source == "gps_rtc_resync"
    assert keeper.writes == [gps]


def test_corrects_rtc_for_one_to_five_minute_drift(tmp_path):
    store = DataStore(tmp_path / "state.sqlite3")
    gps = datetime(2026, 5, 10, 12, 3, tzinfo=timezone.utc)
    rtc = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    keeper = FakeTimeKeeper(TimeConfig(), store, gps, rtc)

    reading = keeper.now()

    assert reading.timestamp == gps
    assert reading.source == "gps_rtc_corrected"
    assert keeper.writes == [gps]
