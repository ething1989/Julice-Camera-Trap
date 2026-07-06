from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
import json
import sqlite3
import time

from .acoustic_indices import ACOUSTIC_INDEX_COLUMNS, ACOUSTIC_INDEX_SQL_TYPES, AcousticIndexResult
from .metrics import diversity_from_counts, format_detection


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class SensorSample:
    sampled_at: datetime
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
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class BirdDetection:
    species: str
    calls: int
    avg_confidence: float | None


@dataclass(frozen=True)
class BirdCandidate:
    species: str
    confidence: float | None


@dataclass(frozen=True)
class BirdCall:
    start_seconds: float | None
    end_seconds: float | None
    candidates: tuple[BirdCandidate, ...]

    @property
    def top_candidate(self) -> BirdCandidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True)
class SoundDetection:
    label: str
    score: float | None
    source: str = "yamnet"
    category: str | None = None


class DataStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        for attempt in range(6):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 5:
                    conn.close()
                    raise
                time.sleep(1 + attempt)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS time_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_timestamp_utc TEXT,
                    bad_gps_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                );

                INSERT OR IGNORE INTO time_state (id, bad_gps_count, updated_at_utc)
                VALUES (1, 0, '1970-01-01T00:00:00+00:00');

                CREATE TABLE IF NOT EXISTS sensor_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at_utc TEXT NOT NULL,
                    temperature_c REAL,
                    humidity_pct REAL,
                    pressure_mmhg REAL,
                    lux REAL,
                    co2_ppm REAL,
                    pm1_0_ug_m3 REAL,
                    pm2_5_ug_m3 REAL,
                    pm10_ug_m3 REAL,
                    particles_0_3_per_l REAL,
                    particles_0_5_per_l REAL,
                    cpu_temp_c REAL
                );

                CREATE INDEX IF NOT EXISTS idx_sensor_samples_time
                ON sensor_samples(sampled_at_utc);

                CREATE TABLE IF NOT EXISTS intervals (
                    period_start_utc TEXT PRIMARY KEY,
                    period_end_utc TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    timestamp_source TEXT NOT NULL,
                    system_event TEXT,
                    temperature_c_avg REAL,
                    humidity_pct_avg REAL,
                    pressure_mmhg_avg REAL,
                    lux_avg REAL,
                    co2_ppm_avg REAL,
                    pm1_0_ug_m3_avg REAL,
                    pm2_5_ug_m3_avg REAL,
                    pm10_ug_m3_avg REAL,
                    particles_0_3_per_l_avg REAL,
                    particles_0_5_per_l_avg REAL,
                    cpu_temp_c_avg REAL,
                    bird_summary TEXT,
                    bird_species_richness INTEGER,
                    bird_total_calls INTEGER,
                    bird_total_species INTEGER,
                    bird_top_species TEXT,
                    bird_shannon_index REAL,
                    bird_simpson_index REAL,
                    bird_pielou_evenness REAL,
                    bird_call_cells TEXT,
                    audio_path TEXT,
                    audio_status TEXT,
                    notes TEXT,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audio_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_utc TEXT NOT NULL UNIQUE,
                    started_at_utc TEXT,
                    ended_at_utc TEXT,
                    path TEXT,
                    status TEXT NOT NULL,
                    ai_status TEXT NOT NULL DEFAULT 'pending',
                    raw_json TEXT,
                    error TEXT,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bird_detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_utc TEXT NOT NULL,
                    species TEXT NOT NULL,
                    calls INTEGER NOT NULL,
                    avg_confidence REAL,
                    UNIQUE(period_start_utc, species)
                );

                CREATE TABLE IF NOT EXISTS bird_call_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_utc TEXT NOT NULL,
                    call_index INTEGER NOT NULL,
                    start_seconds REAL,
                    end_seconds REAL,
                    rank INTEGER NOT NULL,
                    species TEXT NOT NULL,
                    confidence REAL,
                    UNIQUE(period_start_utc, call_index, rank)
                );

                CREATE TABLE IF NOT EXISTS sound_detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_utc TEXT NOT NULL,
                    source TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    score REAL,
                    category TEXT,
                    UNIQUE(period_start_utc, source, rank, label)
                );

                CREATE TABLE IF NOT EXISTS sound_analysis_errors (
                    period_start_utc TEXT NOT NULL,
                    source TEXT NOT NULL,
                    error TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE(period_start_utc, source)
                );

                CREATE TABLE IF NOT EXISTS interval_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start_utc TEXT NOT NULL,
                    error TEXT NOT NULL,
                    source TEXT,
                    details TEXT,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(period_start_utc, error, source, details)
                );

                CREATE INDEX IF NOT EXISTS idx_interval_errors_period
                ON interval_errors(period_start_utc);
                """
            )
            self._init_acoustic_schema(conn)
            self._ensure_column(conn, "intervals", "bird_call_cells", "TEXT")
            self._ensure_column(conn, "intervals", "system_event", "TEXT")
            self._ensure_column(conn, "sensor_samples", "co2_ppm", "REAL")
            self._ensure_column(conn, "intervals", "co2_ppm_avg", "REAL")
            for table in ("sensor_samples", "intervals"):
                suffix = "_avg" if table == "intervals" else ""
                self._ensure_column(conn, table, f"pm1_0_ug_m3{suffix}", "REAL")
                self._ensure_column(conn, table, f"pm2_5_ug_m3{suffix}", "REAL")
                self._ensure_column(conn, table, f"pm10_ug_m3{suffix}", "REAL")
                self._ensure_column(conn, table, f"particles_0_3_per_l{suffix}", "REAL")
                self._ensure_column(conn, table, f"particles_0_5_per_l{suffix}", "REAL")
            for column in ACOUSTIC_INDEX_COLUMNS:
                self._ensure_column(conn, "intervals", column, ACOUSTIC_INDEX_SQL_TYPES[column])

    def _init_acoustic_schema(self, conn: sqlite3.Connection) -> None:
        column_sql = ",\n                    ".join(
            f"{column} {ACOUSTIC_INDEX_SQL_TYPES[column]}" for column in ACOUSTIC_INDEX_COLUMNS
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS acoustic_indices (
                period_start_utc TEXT PRIMARY KEY,
                {column_sql},
                updated_at_utc TEXT NOT NULL
            )
            """
        )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def get_time_state(self) -> sqlite3.Row:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM time_state WHERE id = 1").fetchone()

    def update_time_state(self, last_timestamp: datetime, bad_gps_count: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE time_state
                SET last_timestamp_utc = ?, bad_gps_count = ?, updated_at_utc = ?
                WHERE id = 1
                """,
                (to_utc_iso(last_timestamp), bad_gps_count, to_utc_iso(utc_now())),
            )

    def insert_sensor_sample(self, sample: SensorSample) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sensor_samples (
                    sampled_at_utc, temperature_c, humidity_pct, pressure_mmhg, lux, co2_ppm,
                    pm1_0_ug_m3, pm2_5_ug_m3, pm10_ug_m3, particles_0_3_per_l, particles_0_5_per_l,
                    cpu_temp_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_utc_iso(sample.sampled_at),
                    sample.temperature_c,
                    sample.humidity_pct,
                    sample.pressure_mmhg,
                    sample.lux,
                    sample.co2_ppm,
                    sample.pm1_0_ug_m3,
                    sample.pm2_5_ug_m3,
                    sample.pm10_ug_m3,
                    sample.particles_0_3_per_l,
                    sample.particles_0_5_per_l,
                    sample.cpu_temp_c,
                ),
            )

    def add_interval_error(
        self,
        period_start: datetime,
        error: str,
        source: str | None = None,
        details: str | None = None,
    ) -> None:
        with self.connect() as conn:
            self._insert_interval_error_conn(conn, period_start, error, source, details)

    def _insert_interval_error_conn(
        self,
        conn: sqlite3.Connection,
        period_start: datetime,
        error: str,
        source: str | None,
        details: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO interval_errors (
                period_start_utc, error, source, details, created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (to_utc_iso(period_start), error, source, details, to_utc_iso(utc_now())),
        )

    def aggregate_sensor_samples(self, start: datetime, end: datetime) -> dict[str, float | None]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    AVG(temperature_c) AS temperature_c_avg,
                    AVG(humidity_pct) AS humidity_pct_avg,
                    AVG(pressure_mmhg) AS pressure_mmhg_avg,
                    AVG(lux) AS lux_avg,
                    AVG(co2_ppm) AS co2_ppm_avg,
                    AVG(pm1_0_ug_m3) AS pm1_0_ug_m3_avg,
                    AVG(pm2_5_ug_m3) AS pm2_5_ug_m3_avg,
                    AVG(pm10_ug_m3) AS pm10_ug_m3_avg,
                    AVG(particles_0_3_per_l) AS particles_0_3_per_l_avg,
                    AVG(particles_0_5_per_l) AS particles_0_5_per_l_avg,
                    AVG(cpu_temp_c) AS cpu_temp_c_avg
                FROM sensor_samples
                WHERE sampled_at_utc >= ? AND sampled_at_utc < ?
                """,
                (to_utc_iso(start), to_utc_iso(end)),
            ).fetchone()
        return dict(row)

    def upsert_audio_event(
        self,
        period_start: datetime,
        status: str,
        path: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        ai_status: str | None = None,
        raw_json: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM audio_events WHERE period_start_utc = ?", (to_utc_iso(period_start),)
            ).fetchone()
            next_ai_status = ai_status or (existing["ai_status"] if existing else "pending")
            started_iso = to_utc_iso(started_at) if started_at else (existing["started_at_utc"] if existing else None)
            ended_iso = to_utc_iso(ended_at) if ended_at else (existing["ended_at_utc"] if existing else None)
            next_path = path if path is not None else (existing["path"] if existing else None)
            next_raw = json.dumps(raw_json) if raw_json is not None else (existing["raw_json"] if existing else None)
            if error is not None:
                next_error = error
            elif ai_status == "done":
                next_error = None
            else:
                next_error = existing["error"] if existing else None
            conn.execute(
                """
                INSERT INTO audio_events (
                    period_start_utc, started_at_utc, ended_at_utc, path, status, ai_status,
                    raw_json, error, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_start_utc) DO UPDATE SET
                    started_at_utc = excluded.started_at_utc,
                    ended_at_utc = excluded.ended_at_utc,
                    path = excluded.path,
                    status = excluded.status,
                    ai_status = excluded.ai_status,
                    raw_json = excluded.raw_json,
                    error = excluded.error,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    to_utc_iso(period_start),
                    started_iso,
                    ended_iso,
                    next_path,
                    status,
                    next_ai_status,
                    next_raw,
                    next_error,
                    to_utc_iso(utc_now()),
                ),
            )

    def save_bird_detections(self, period_start: datetime, detections: list[BirdDetection]) -> None:
        calls: list[BirdCall] = []
        call_index = 0
        for detection in detections:
            for _ in range(max(0, detection.calls)):
                call_index += 1
                calls.append(
                    BirdCall(
                        start_seconds=None,
                        end_seconds=None,
                        candidates=(BirdCandidate(detection.species, detection.avg_confidence),),
                    )
                )
        self.save_bird_calls(period_start, calls)

    def save_bird_calls(self, period_start: datetime, calls: list[BirdCall]) -> None:
        detections = calls_to_detections(calls)
        with self.connect() as conn:
            conn.execute("DELETE FROM bird_detections WHERE period_start_utc = ?", (to_utc_iso(period_start),))
            conn.execute("DELETE FROM bird_call_candidates WHERE period_start_utc = ?", (to_utc_iso(period_start),))
            conn.executemany(
                """
                INSERT INTO bird_detections (period_start_utc, species, calls, avg_confidence)
                VALUES (?, ?, ?, ?)
                """,
                [(to_utc_iso(period_start), d.species, d.calls, d.avg_confidence) for d in detections],
            )
            conn.executemany(
                """
                INSERT INTO bird_call_candidates (
                    period_start_utc, call_index, start_seconds, end_seconds, rank, species, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        to_utc_iso(period_start),
                        call_index,
                        call.start_seconds,
                        call.end_seconds,
                        rank,
                        candidate.species,
                        candidate.confidence,
                    )
                    for call_index, call in enumerate(calls, start=1)
                    for rank, candidate in enumerate(call.candidates, start=1)
                ],
            )
            conn.execute(
                """
                UPDATE audio_events
                SET ai_status = 'done', updated_at_utc = ?
                WHERE period_start_utc = ?
                """,
                (to_utc_iso(utc_now()), to_utc_iso(period_start)),
            )

    def save_sound_detections(
        self,
        period_start: datetime,
        source: str,
        detections: list[SoundDetection],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM sound_detections WHERE period_start_utc = ? AND source = ?",
                (to_utc_iso(period_start), source),
            )
            conn.execute(
                "DELETE FROM sound_analysis_errors WHERE period_start_utc = ? AND source = ?",
                (to_utc_iso(period_start), source),
            )
            conn.executemany(
                """
                INSERT INTO sound_detections (
                    period_start_utc, source, rank, label, score, category
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        to_utc_iso(period_start),
                        source,
                        index,
                        detection.label,
                        detection.score,
                        detection.category,
                    )
                    for index, detection in enumerate(detections, start=1)
                ],
            )

    def save_sound_analysis_error(self, period_start: datetime, source: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM sound_detections WHERE period_start_utc = ? AND source = ?",
                (to_utc_iso(period_start), source),
            )
            conn.execute(
                """
                INSERT INTO sound_analysis_errors (
                    period_start_utc, source, error, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(period_start_utc, source) DO UPDATE SET
                    error = excluded.error,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (to_utc_iso(period_start), source, error, to_utc_iso(utc_now())),
            )

    def save_acoustic_indices(self, period_start: datetime, indices: AcousticIndexResult) -> None:
        columns = ", ".join(ACOUSTIC_INDEX_COLUMNS)
        placeholders = ", ".join("?" for _ in ACOUSTIC_INDEX_COLUMNS)
        assignments = ", ".join(f"{column} = excluded.{column}" for column in ACOUSTIC_INDEX_COLUMNS)
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO acoustic_indices (
                    period_start_utc, {columns}, updated_at_utc
                ) VALUES (?, {placeholders}, ?)
                ON CONFLICT(period_start_utc) DO UPDATE SET
                    {assignments},
                    updated_at_utc = excluded.updated_at_utc
                """,
                (to_utc_iso(period_start), *indices.as_db_values(), to_utc_iso(utc_now())),
            )

    def pending_audio_events(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM audio_events
                    WHERE status = 'recorded' AND ai_status IN ('pending', 'retry')
                    ORDER BY period_start_utc ASC
                    """
                )
            )

    def audio_event_for_path(self, path: Path) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM audio_events WHERE path = ?", (str(path),)).fetchone()

    def insert_system_event(self, timestamp: datetime, event: str, timestamp_source: str = "system") -> None:
        period_start = timestamp.astimezone(UTC)
        period_end = period_start
        with self.connect() as conn:
            key = to_utc_iso(period_start)
            suffix = 0
            while conn.execute("SELECT 1 FROM intervals WHERE period_start_utc = ?", (key,)).fetchone():
                suffix += 1
                key = to_utc_iso(period_start + timedelta(seconds=suffix))
            conn.execute(
                """
                INSERT INTO intervals (
                    period_start_utc, period_end_utc, timestamp_utc, timestamp_source,
                    system_event, audio_status, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, '', ?)
                """,
                (
                    key,
                    to_utc_iso(period_end),
                    to_utc_iso(timestamp),
                    timestamp_source,
                    event,
                    to_utc_iso(utc_now()),
                ),
            )

    def upsert_interval_event(
        self,
        period_start: datetime,
        period_end: datetime,
        timestamp: datetime,
        timestamp_source: str,
        event: str,
    ) -> None:
        self.upsert_interval_summary(period_start, period_end, timestamp, timestamp_source)
        self.set_interval_system_event(period_start, event)

    def upsert_interval_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        timestamp: datetime,
        timestamp_source: str,
        notes: str | None = None,
    ) -> None:
        sensor = self.aggregate_sensor_samples(period_start, period_end)
        birds = self._bird_summary(period_start)
        audio = self._audio_summary(period_start)
        acoustic = self._acoustic_summary(period_start)
        interval_values = {
            "period_start_utc": to_utc_iso(period_start),
            "period_end_utc": to_utc_iso(period_end),
            "timestamp_utc": to_utc_iso(timestamp),
            "timestamp_source": timestamp_source,
            "system_event": None,
            "temperature_c_avg": sensor["temperature_c_avg"],
            "humidity_pct_avg": sensor["humidity_pct_avg"],
            "pressure_mmhg_avg": sensor["pressure_mmhg_avg"],
            "lux_avg": sensor["lux_avg"],
            "co2_ppm_avg": sensor["co2_ppm_avg"],
            "pm1_0_ug_m3_avg": sensor["pm1_0_ug_m3_avg"],
            "pm2_5_ug_m3_avg": sensor["pm2_5_ug_m3_avg"],
            "pm10_ug_m3_avg": sensor["pm10_ug_m3_avg"],
            "particles_0_3_per_l_avg": sensor["particles_0_3_per_l_avg"],
            "particles_0_5_per_l_avg": sensor["particles_0_5_per_l_avg"],
            "cpu_temp_c_avg": sensor["cpu_temp_c_avg"],
            "bird_summary": birds["summary"],
            "bird_species_richness": birds["metrics"].species_richness,
            "bird_total_calls": birds["metrics"].total_calls,
            "bird_total_species": birds["metrics"].species_richness,
            "bird_top_species": birds["top_species"],
            "bird_shannon_index": birds["metrics"].shannon,
            "bird_simpson_index": birds["metrics"].simpson,
            "bird_pielou_evenness": birds["metrics"].pielou_evenness,
            "bird_call_cells": json.dumps(birds["call_cells"]),
            "audio_path": audio["path"],
            "audio_status": audio["status"],
            "notes": notes,
            "updated_at_utc": to_utc_iso(utc_now()),
        }
        interval_values.update({column: acoustic[column] for column in ACOUSTIC_INDEX_COLUMNS})
        columns = tuple(interval_values)
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        assignments = []
        for column in columns:
            if column == "period_start_utc":
                continue
            if column == "system_event":
                assignments.append("system_event = COALESCE(intervals.system_event, excluded.system_event)")
            else:
                assignments.append(f"{column} = excluded.{column}")
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO intervals ({column_sql})
                VALUES ({placeholders})
                ON CONFLICT(period_start_utc) DO UPDATE SET
                    {", ".join(assignments)}
                """,
                tuple(interval_values[column] for column in columns),
            )

    def refresh_interval_summary(self, period_start: datetime, default_period_seconds: int = 300) -> None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT period_end_utc, timestamp_utc, timestamp_source, notes
                FROM intervals
                WHERE period_start_utc = ?
                """,
                (to_utc_iso(period_start),),
            ).fetchone()
        if row is None:
            period_end = period_start + timedelta(seconds=default_period_seconds)
            self.upsert_interval_summary(period_start, period_end, period_start, "backfill")
            return
        self.upsert_interval_summary(
            period_start,
            from_iso(row["period_end_utc"]),
            from_iso(row["timestamp_utc"]),
            row["timestamp_source"],
            row["notes"],
        )

    def set_interval_system_event(self, period_start: datetime, event: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT system_event FROM intervals WHERE period_start_utc = ?",
                (to_utc_iso(period_start),),
            ).fetchone()
            existing = row["system_event"] if row else None
            next_event = _append_event(existing, event)
            conn.execute(
                """
                UPDATE intervals
                SET system_event = ?, updated_at_utc = ?
                WHERE period_start_utc = ?
                """,
                (next_event, to_utc_iso(utc_now()), to_utc_iso(period_start)),
            )

    def _audio_summary(self, period_start: datetime) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT path, status, ai_status, error FROM audio_events WHERE period_start_utc = ?",
                (to_utc_iso(period_start),),
            ).fetchone()
        if row is None:
            return {"path": None, "status": ""}
        status = row["status"]
        if row["ai_status"] not in ("done", "pending"):
            status = f"{status}/{row['ai_status']}"
        return {"path": row["path"], "status": status}

    def _acoustic_summary(self, period_start: datetime) -> dict[str, Any]:
        column_sql = ", ".join(ACOUSTIC_INDEX_COLUMNS)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {column_sql} FROM acoustic_indices WHERE period_start_utc = ?",
                (to_utc_iso(period_start),),
            ).fetchone()
        if row is None:
            return {column: None for column in ACOUSTIC_INDEX_COLUMNS}
        return {column: row[column] for column in ACOUSTIC_INDEX_COLUMNS}

    def _bird_summary(self, period_start: datetime) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT species, calls, avg_confidence
                FROM bird_detections
                WHERE period_start_utc = ?
                ORDER BY calls DESC, avg_confidence DESC, species ASC
                """,
                (to_utc_iso(period_start),),
            ).fetchall()
            call_rows = conn.execute(
                """
                SELECT call_index, rank, species, confidence
                FROM bird_call_candidates
                WHERE period_start_utc = ?
                ORDER BY call_index ASC, rank ASC
                """,
                (to_utc_iso(period_start),),
            ).fetchall()
        counts = {row["species"]: int(row["calls"]) for row in rows}
        metrics = diversity_from_counts(counts)
        summary = "; ".join(
            format_detection(row["species"], "Calls", int(row["calls"]), row["avg_confidence"]) for row in rows
        )
        top_species = _format_top_species(rows[0]) if rows else None
        calls_by_index: dict[int, list[sqlite3.Row]] = {}
        for row in call_rows:
            calls_by_index.setdefault(int(row["call_index"]), []).append(row)
        call_cells = [_format_call_cell(rows) for _index, rows in sorted(calls_by_index.items())]
        return {"summary": summary, "metrics": metrics, "call_cells": call_cells, "top_species": top_species}

    def list_intervals_for_day(self, day_start: datetime, day_end: datetime) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM intervals
                    WHERE period_start_utc >= ? AND period_start_utc < ?
                    ORDER BY period_start_utc ASC
                    """,
                    (to_utc_iso(day_start), to_utc_iso(day_end)),
                )
            )

    def list_intervals(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM intervals ORDER BY period_start_utc ASC"))

    def list_bird_call_candidates(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT period_start_utc, call_index, start_seconds, end_seconds, rank, species, confidence
                    FROM bird_call_candidates
                    ORDER BY period_start_utc ASC, call_index ASC, rank ASC
                    """
                )
            )

    def list_sound_detections(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT period_start_utc, source, rank, label, score, category
                    FROM sound_detections
                    ORDER BY period_start_utc ASC, source ASC, rank ASC, score DESC, label ASC
                    """
                )
            )

    def list_sound_analysis_errors(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT period_start_utc, source, error
                    FROM sound_analysis_errors
                    ORDER BY period_start_utc ASC, source ASC
                    """
                )
            )

    def list_interval_errors(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT period_start_utc, error, source, details
                    FROM interval_errors
                    ORDER BY period_start_utc ASC, error ASC, source ASC, details ASC
                    """
                )
            )


def calls_to_detections(calls: list[BirdCall]) -> list[BirdDetection]:
    grouped: dict[str, list[float | None]] = {}
    for call in calls:
        top = call.top_candidate
        if top is None or not top.species:
            continue
        grouped.setdefault(top.species, []).append(top.confidence)

    detections: list[BirdDetection] = []
    for species, confidences in grouped.items():
        numeric = [value for value in confidences if value is not None]
        avg = sum(numeric) / len(numeric) if numeric else None
        detections.append(BirdDetection(species=species, calls=len(confidences), avg_confidence=avg))
    return sorted(detections, key=lambda item: (-item.calls, item.species))


def _format_call_cell(rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in rows:
        confidence = row["confidence"]
        if confidence is None:
            lines.append(str(row["species"]))
        else:
            lines.append(f"{row['species']} ({confidence * 100:.1f}%)")
    return "\n".join(lines)


def _format_top_species(row: sqlite3.Row) -> str:
    confidence = row["avg_confidence"]
    if confidence is None:
        return f"{row['species']}(Calls: {int(row['calls'])}, Conf: )"
    return f"{row['species']}(Calls: {int(row['calls'])}, Conf: {confidence * 100:.1f}%)"


def _append_event(existing: str | None, event: str) -> str:
    event = event.strip()
    if not event:
        return existing or ""
    parts = [part.strip() for part in (existing or "").replace("\n", ";").split(";") if part.strip()]
    if event not in parts:
        parts.append(event)
    return ";".join(parts)
