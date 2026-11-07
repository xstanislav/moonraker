from __future__ import annotations

import hashlib
import zipfile
from concurrent.futures import Future
from pathlib import Path

import pytest

from moonraker.components.file_manager.file_manager import FileManager
from moonraker.components.job_queue import QueuedJob
from moonraker.utils.exceptions import ServerError


GCODE_1 = b"G28\nG1 X10 Y10\n"
GCODE_3 = b"G28\nG1 X20 Y20\n"


def build_archive(path: Path) -> None:
    model_settings = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value="First plate"/>
  </plate>
  <plate>
    <metadata key="plater_id" value="3"/>
    <metadata key="plater_name" value="Third plate"/>
  </plate>
</config>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/model_settings.config", model_settings)
        archive.writestr("Metadata/plate_1.gcode", GCODE_1)
        archive.writestr("Metadata/plate_3.gcode", GCODE_3)
        archive.writestr(
            "Metadata/plate_1.gcode.md5",
            hashlib.md5(GCODE_1).hexdigest().upper(),
        )
        archive.writestr(
            "Metadata/plate_3.gcode.md5",
            hashlib.md5(GCODE_3).hexdigest().upper(),
        )


class FakeServer:
    @staticmethod
    def error(message: str, status_code: int = 400) -> ServerError:
        return ServerError(message, status_code)


class FakeFileManager:
    server = FakeServer()
    file_paths: dict[str, str] = {}

    _normalize_source_rel = FileManager._normalize_source_rel
    check_plate_exists = FileManager.check_plate_exists
    _parse_upload_args = FileManager._parse_upload_args


@pytest.fixture
def file_manager(tmp_path: Path) -> FakeFileManager:
    file_manager = FakeFileManager()
    file_manager.file_paths = {"gcodes": str(tmp_path)}
    return file_manager


def test_check_plate_exists(file_manager: FakeFileManager, tmp_path: Path) -> None:
    build_archive(tmp_path / "part.gcode.3mf")
    (tmp_path / "part.gcode").write_bytes(GCODE_1)

    assert file_manager.check_plate_exists("part.gcode.3mf", 3) == 3
    assert file_manager.check_plate_exists("gcodes/part.gcode.3mf", 1) == 1
    assert file_manager.check_plate_exists("part.gcode", None) == 0

    with pytest.raises(ServerError, match="plate index is required"):
        file_manager.check_plate_exists("part.gcode.3mf", None)
    with pytest.raises(ServerError, match="Plate 9 is not available"):
        file_manager.check_plate_exists("part.gcode.3mf", 9)
    with pytest.raises(ServerError, match="only valid for .gcode.3mf"):
        file_manager.check_plate_exists("part.gcode", 1)
    with pytest.raises(ServerError, match="does not exist"):
        file_manager.check_plate_exists("missing.gcode.3mf", 1)


def test_parse_upload_args_plate(
    file_manager: FakeFileManager, tmp_path: Path
) -> None:
    common = {
        "root": "gcodes",
        "filename": "part.gcode.3mf",
        "tmp_file_path": str(tmp_path / "upload.tmp"),
    }

    parsed = file_manager._parse_upload_args({**common, "plate": "3"})
    assert parsed["multi_plate"] is True
    assert parsed["plate"] == 3
    assert parsed["dest_path"] == str(tmp_path / "part.gcode.3mf")

    parsed = file_manager._parse_upload_args(common)
    assert parsed["multi_plate"] is True
    assert parsed["plate"] is None

    parsed = file_manager._parse_upload_args(
        {**common, "filename": "project.3mf"}
    )
    assert parsed["multi_plate"] is False

    with pytest.raises(ServerError, match="Invalid plate index"):
        file_manager._parse_upload_args({**common, "plate": "third"})


def test_metadata_cleanup_schedule() -> None:
    class FakeEventLoop:
        def __init__(self) -> None:
            self.tasks: list[object] = []

        def create_task(self, coro: object) -> None:
            self.tasks.append(coro)
            coro.close()  # type: ignore[attr-defined]

    file_manager = FileManager.__new__(FileManager)
    file_manager.event_loop = FakeEventLoop()  # type: ignore[assignment]

    async def cleanup() -> None:
        pass

    coroutine = cleanup()
    file_manager._schedule_metadata_cleanup(coroutine)
    assert file_manager.event_loop.tasks == [coroutine]

    future: Future[None] = Future()
    file_manager._schedule_metadata_cleanup(future)
    assert len(file_manager.event_loop.tasks) == 1
    file_manager._schedule_metadata_cleanup(None)
    assert len(file_manager.event_loop.tasks) == 1


def test_queued_job_plate_metadata() -> None:
    job = QueuedJob("part.gcode.3mf", 3)
    job.plate_name = "Third plate"

    data = job.as_dict(100.0)

    assert data["filename"] == "part.gcode.3mf"
    assert data["source_filename"] == "part.gcode.3mf"
    assert data["plate"] == 3
    assert data["plate_name"] == "Third plate"
    assert data["job_id"] == job.job_id

    plain_job = QueuedJob("part.gcode")
    assert plain_job.as_dict(100.0)["plate"] is None
    assert plain_job.as_dict(100.0)["plate_name"] is None
