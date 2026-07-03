from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil

from .config import StorageConfig


@dataclass(frozen=True)
class StationPaths:
    root: Path
    fallback_root: Path
    state_root: Path | None = None
    work_root: Path | None = None
    recording_root: Path | None = None
    logs_subdir: str = "logs"
    fallback_active: bool = False

    def _subdir_path(self, value: str) -> Path:
        value = str(value).strip()
        if value in {"", "."}:
            return self.root
        return self.root / value

    @property
    def state_dir(self) -> Path:
        return self.state_root or (self.root / "state")

    @property
    def logs_dir(self) -> Path:
        return self._subdir_path(self.logs_subdir)

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def audio_dir(self) -> Path:
        return self.media_dir / "audio"

    @property
    def recordings_dir(self) -> Path:
        if self.recording_root is not None:
            return self.recording_root
        if self.work_root is not None:
            return self.work_root / "audio_recordings"
        return self.fallback_root / "audio_recordings"

    @property
    def ai_work_dir(self) -> Path:
        return self.work_root or (self.root / "ai_work")

    @property
    def database_path(self) -> Path:
        return self.state_dir / "station.sqlite3"

    def ensure(self) -> None:
        for path in [self.state_dir, self.logs_dir, self.ai_work_dir, self.recordings_dir]:
            path.mkdir(parents=True, exist_ok=True)
        if self.audio_dir != self.recordings_dir:
            shutil.rmtree(self.audio_dir, ignore_errors=True)


def resolve_paths(storage: StorageConfig) -> StationPaths:
    root_mount_ready = _storage_root_mount_ready(storage.root)
    if root_mount_ready and _is_writable_dir(storage.root):
        paths = StationPaths(
            storage.root,
            storage.fallback_root,
            storage.state_root,
            storage.work_root,
            storage.recording_root,
            storage.logs_subdir,
            fallback_active=False,
        )
        paths.ensure()
        return paths
    if storage.require_usb:
        if not root_mount_ready:
            raise RuntimeError(f"Configured USB mount is not active for root: {storage.root}")
        raise RuntimeError(f"Configured USB root is not writable: {storage.root}")
    fallback = StationPaths(
        storage.fallback_root,
        storage.fallback_root,
        storage.state_root,
        storage.work_root,
        storage.recording_root,
        storage.logs_subdir,
        fallback_active=True,
    )
    fallback.ensure()
    return fallback


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test = path / ".write_test"
        with test.open("w") as handle:
            handle.write("ok")
        test.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _storage_root_mount_ready(path: Path) -> bool:
    mount_root = _mount_root_for_storage(path)
    return mount_root is None or mount_root.is_mount()


def _mount_root_for_storage(path: Path) -> Path | None:
    parts = path.expanduser().absolute().parts
    if len(parts) >= 3 and parts[1] in {"mnt", "media"}:
        return Path(parts[0], parts[1], parts[2])
    return None


def atomic_replace_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text)
    os.replace(temp, path)
