# Multi-plate 3MF support for Moonraker
#
# Copyright (C) 2026 Mainsail contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# OrcaSlicer and Bambu Studio store sliced multi-plate projects as ZIP
# archives with the ".gcode.3mf" suffix.  Each plate is stored as
# "Metadata/plate_<n>.gcode", while plate metadata lives in
# "Metadata/model_settings.config" and "Metadata/slice_info.config".
#
# This module intentionally has no Moonraker imports so that it can be
# executed from metadata.py (which runs in a subprocess) and unit tested
# without a running server.

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a Moonraker dependency
    Image = None  # type: ignore[assignment]

MULTI_PLATE_SUFFIX = ".gcode.3mf"
PLATE_GCODE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$", re.IGNORECASE)
PLATE_MD5_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode\.md5$", re.IGNORECASE)

MAX_ENTRIES = 10000
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024
MAX_METADATA_ENTRY_SIZE = 50 * 1024 * 1024
MAX_THUMBNAIL_ENTRY_SIZE = 50 * 1024 * 1024
MAX_MD5_ENTRY_SIZE = 1024
MAX_COMPRESSION_RATIO = 1000

THUMBNAIL_KEYS = (
    ("thumbnail_file", "thumbnail"),
    ("thumbnail_no_light_file", "no_light"),
    ("top_file", "top"),
    ("pick_file", "pick"),
)


class ThreeMfError(Exception):
    pass


def is_multi_plate_file(filename: str) -> bool:
    return filename.lower().endswith(MULTI_PLATE_SUFFIX)


def strip_multi_plate_suffix(filename: str) -> str:
    if is_multi_plate_file(filename):
        return filename[: -len(MULTI_PLATE_SUFFIX)]
    return os.path.splitext(filename)[0]


def get_file_ext(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(MULTI_PLATE_SUFFIX):
        return MULTI_PLATE_SUFFIX
    return os.path.splitext(lower)[1]


def safe_thumb_base(filename: str) -> str:
    base = strip_multi_plate_suffix(os.path.basename(filename))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return base[:80] or "project"


def _normalize_entry_name(name: str) -> str:
    name = name.replace("\\", "/").lstrip("/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ThreeMfError(f"Unsafe archive entry name: {name}")
    return "/".join(parts)


def _open_zip(path: str | Path) -> Tuple[zipfile.ZipFile, Dict[str, zipfile.ZipInfo]]:
    zf = zipfile.ZipFile(path)
    entries: Dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        zf.close()
        raise ThreeMfError(
            f"Archive contains too many entries ({len(infos)} > {MAX_ENTRIES})"
        )
    for info in infos:
        if info.flag_bits & 0x1:
            zf.close()
            raise ThreeMfError(f"Encrypted archive entries are not supported: {info.filename}")
        name = _normalize_entry_name(info.filename)
        if name in entries:
            zf.close()
            raise ThreeMfError(f"Duplicate archive entry: {name}")
        entries[name] = info
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_SIZE:
            zf.close()
            raise ThreeMfError("Archive exceeds the maximum uncompressed size")
        if (
            info.file_size > MAX_METADATA_ENTRY_SIZE
            and info.compress_size > 0
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            zf.close()
            raise ThreeMfError(
                f"Archive entry has a suspicious compression ratio: {name}"
            )
    return zf, entries


def _find_entry(
    entries: Dict[str, zipfile.ZipInfo], name: Optional[str]
) -> Optional[zipfile.ZipInfo]:
    if not name:
        return None
    normalized = _normalize_entry_name(name)
    info = entries.get(normalized)
    if info is not None:
        return info
    lower = normalized.lower()
    for entry_name, entry_info in entries.items():
        if entry_name.lower() == lower:
            return entry_info
    return None


def _read_entry(
    zf: zipfile.ZipFile,
    entries: Dict[str, zipfile.ZipInfo],
    name: Optional[str],
    max_size: int = MAX_METADATA_ENTRY_SIZE,
) -> Optional[bytes]:
    info = _find_entry(entries, name)
    if info is None:
        return None
    if info.file_size > max_size:
        raise ThreeMfError(f"Archive entry is too large: {info.filename}")
    return zf.read(info)


def _read_text(
    zf: zipfile.ZipFile,
    entries: Dict[str, zipfile.ZipInfo],
    name: Optional[str],
    max_size: int = MAX_METADATA_ENTRY_SIZE,
) -> Optional[str]:
    data = _read_entry(zf, entries, name, max_size)
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_children(element: ET.Element, name: str) -> List[ET.Element]:
    return [child for child in element if _xml_local_name(child.tag) == name]


def _parse_model_settings(data: str) -> List[Dict[str, str]]:
    root = ET.fromstring(data)
    plates: List[Dict[str, str]] = []
    for plate in _xml_children(root, "plate"):
        metadata: Dict[str, str] = {}
        for item in _xml_children(plate, "metadata"):
            key = item.attrib.get("key")
            if key:
                metadata[key] = item.attrib.get("value", "")
        if metadata:
            plates.append(metadata)
    return plates


def _parse_slice_info(data: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    root = ET.fromstring(data)
    header: Dict[str, str] = {}
    for header_elem in _xml_children(root, "header"):
        for item in _xml_children(header_elem, "header_item"):
            key = item.attrib.get("key")
            if key:
                header[key] = item.attrib.get("value", "")
    plates: List[Dict[str, Any]] = []
    for plate in _xml_children(root, "plate"):
        metadata: Dict[str, str] = {}
        for item in _xml_children(plate, "metadata"):
            key = item.attrib.get("key")
            if key:
                metadata[key] = item.attrib.get("value", "")
        filaments = [
            dict(item.attrib) for item in _xml_children(plate, "filament")
        ]
        objects = [dict(item.attrib) for item in _xml_children(plate, "object")]
        warnings = [dict(item.attrib) for item in _xml_children(plate, "warning")]
        nozzles = [dict(item.attrib) for item in _xml_children(plate, "nozzle")]
        plates.append(
            {
                "metadata": metadata,
                "filaments": filaments,
                "objects": objects,
                "warnings": warnings,
                "nozzles": nozzles,
            }
        )
    return header, plates


def _parse_model_metadata(data: str) -> Dict[str, str]:
    root = ET.fromstring(data)
    metadata: Dict[str, str] = {}
    for item in root.iter():
        if _xml_local_name(item.tag) != "metadata":
            continue
        name = item.attrib.get("name")
        if name:
            metadata[name] = (item.text or "").strip()
    return metadata


def _parse_plate_json(data: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _unique(values: List[Any]) -> List[Any]:
    result: List[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _collapse(values: List[Any]) -> Any:
    values = _unique([value for value in values if value not in (None, "")])
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return json.dumps(values)


def _setting(settings: Dict[str, Any], key: str, index: int = 0) -> Any:
    value = settings.get(key)
    if isinstance(value, list):
        if index < len(value):
            return value[index]
        return None
    return value


def _first_setting(settings: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = _setting(settings, key)
        if value not in (None, ""):
            return value
    return None


def _bed_temp(settings: Dict[str, Any]) -> Optional[float]:
    return _to_float(
        _first_setting(
            settings,
            "hot_plate_temp_initial_layer",
            "textured_plate_temp_initial_layer",
            "cool_plate_temp_initial_layer",
            "eng_plate_temp_initial_layer",
            "supertack_plate_temp_initial_layer",
            "hot_plate_temp",
        )
    )


def _image_size(data: bytes) -> Optional[Tuple[int, int]]:
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                return image.size
        except Exception:
            pass
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        except struct.error:
            return None
    return None


def _make_small_thumbnail(
    source_path: Path, destination: Path, size: int = 32
) -> Optional[Tuple[int, int, int]]:
    if Image is None or not source_path.is_file():
        return None
    try:
        with Image.open(source_path) as image:
            image.thumbnail((size, size))
            image.save(destination, format="PNG")
            return image.width, image.height, destination.stat().st_size
    except Exception:
        return None


def _plate_thumbnail_candidates(
    plate_meta: Dict[str, str], index: int
) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    for key, kind in THUMBNAIL_KEYS:
        value = plate_meta.get(key)
        if value:
            candidates.append((value, kind))
    defaults = (
        (f"Metadata/plate_{index}.png", "thumbnail"),
        (f"Metadata/plate_no_light_{index}.png", "no_light"),
        (f"Metadata/top_{index}.png", "top"),
        (f"Metadata/pick_{index}.png", "pick"),
    )
    seen = {name.lower() for name, _ in candidates}
    for name, kind in defaults:
        if name.lower() not in seen:
            candidates.append((name, kind))
            seen.add(name.lower())
    return candidates


def _extract_thumbnails(
    zf: zipfile.ZipFile,
    entries: Dict[str, zipfile.ZipInfo],
    plates: List[Dict[str, Any]],
    thumb_dir: Path,
    base_name: str,
) -> List[Dict[str, Any]]:
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumbnails: List[Dict[str, Any]] = []
    for plate in plates:
        index = plate["index"]
        plate_thumbs: List[Dict[str, Any]] = []
        large_thumb_path: Optional[Path] = None
        for entry_name, kind in _plate_thumbnail_candidates(plate, index):
            info = _find_entry(entries, entry_name)
            if info is None or info.file_size > MAX_THUMBNAIL_ENTRY_SIZE:
                continue
            try:
                data = zf.read(info)
                dimensions = _image_size(data)
            except Exception:
                continue
            if dimensions is None:
                continue
            width, height = dimensions
            suffix = f"-{kind}" if kind != "thumbnail" else ""
            dest_name = f"{base_name}-plate_{index}{suffix}-{width}x{height}.png"
            dest_path = thumb_dir / dest_name
            if not dest_path.is_file() or dest_path.stat().st_size != len(data):
                dest_path.write_bytes(data)
            rel_path = f".thumbs/{dest_name}"
            thumb = {
                "width": width,
                "height": height,
                "size": len(data),
                "relative_path": rel_path,
                "plate": index,
                "type": kind,
            }
            plate_thumbs.append(thumb)
            thumbnails.append(thumb)
            if kind == "thumbnail" and width >= 128:
                large_thumb_path = dest_path
        if large_thumb_path is not None and not any(
            thumb["type"] == "thumbnail" and thumb["width"] <= 64
            for thumb in plate_thumbs
        ):
            small_name = f"{base_name}-plate_{index}-32x32.png"
            small_path = thumb_dir / small_name
            small = _make_small_thumbnail(large_thumb_path, small_path)
            if small is not None:
                width, height, size = small
                thumb = {
                    "width": width,
                    "height": height,
                    "size": size,
                    "relative_path": f".thumbs/{small_name}",
                    "plate": index,
                    "type": "thumbnail",
                }
                plate_thumbs.append(thumb)
                thumbnails.append(thumb)
        plate["thumbnails"] = plate_thumbs
    return thumbnails


def _find_gcode_entries(entries: Dict[str, zipfile.ZipInfo]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for name in entries:
        match = PLATE_GCODE_RE.match(name)
        if match:
            result[int(match.group(1))] = name
    return result


def _find_md5_entries(entries: Dict[str, zipfile.ZipInfo]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for name in entries:
        match = PLATE_MD5_RE.match(name)
        if match:
            result[int(match.group(1))] = name
    return result


def _build_plate(
    index: int,
    plate_meta: Dict[str, str],
    slice_info: Optional[Dict[str, Any]],
    plate_json: Dict[str, Any],
    gcode_entry: Optional[str],
    gcode_info: Optional[zipfile.ZipInfo],
    md5_entry: Optional[str],
    md5_value: Optional[str],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    info = slice_info or {}
    metadata = info.get("metadata", {}) if info else {}
    filaments = info.get("filaments", []) if info else []
    objects = info.get("objects", []) if info else []
    warnings = info.get("warnings", []) if info else []
    nozzles = info.get("nozzles", []) if info else []

    filament_types = [
        str(filament.get("type"))
        for filament in filaments
        if filament.get("type")
    ]
    filament_colors = [
        str(filament.get("color"))
        for filament in filaments
        if filament.get("color")
    ]
    filament_names = [
        str(filament.get("name"))
        for filament in filaments
        if filament.get("name")
    ]
    filament_weights = [
        _to_float(filament.get("used_g"))
        for filament in filaments
        if filament.get("used_g") is not None
    ]
    filament_weights = [weight for weight in filament_weights if weight is not None]
    referenced_tools: List[int] = []
    for filament in filaments:
        filament_id = _to_int(filament.get("id"))
        if filament_id is not None:
            referenced_tools.append(max(filament_id - 1, 0))

    if not filament_types:
        value = _setting(settings, "filament_type")
        if isinstance(value, list):
            filament_types = [str(item) for item in value]
        elif value is not None:
            filament_types = [str(value)]
    if not filament_colors:
        value = plate_json.get("filament_colors")
        if isinstance(value, list):
            filament_colors = [str(item) for item in value]
        elif value is not None:
            filament_colors = [str(value)]
    if not filament_colors:
        value = _setting(settings, "filament_colour")
        if isinstance(value, list):
            filament_colors = [str(item) for item in value]
        elif value is not None:
            filament_colors = [str(value)]
    if not filament_names:
        value = _setting(settings, "filament_settings_id")
        if isinstance(value, list):
            filament_names = [str(item) for item in value]
        elif value is not None:
            filament_names = [str(value)]

    printable = gcode_entry is not None and gcode_info is not None
    object_count = len(
        [obj for obj in objects if not _to_bool(obj.get("skipped", "false"))]
    )
    bbox_objects = plate_json.get("bbox_objects")
    if object_count == 0 and isinstance(bbox_objects, list):
        object_count = len(bbox_objects)
    return {
        "index": index,
        "name": plate_meta.get("plater_name") or f"Plate {index}",
        "gcode_filename": gcode_entry,
        "gcode_size": gcode_info.file_size if gcode_info is not None else 0,
        "gcode_md5": md5_value,
        "gcode_md5_file": md5_entry,
        "prediction": _to_float(metadata.get("prediction")),
        "weight": _to_float(metadata.get("weight")),
        "first_layer_time": _to_float(metadata.get("first_layer_time")),
        "filament_type": _unique(filament_types),
        "filament_colors": _unique(filament_colors),
        "filament_names": _unique(filament_names),
        "filament_weights": filament_weights,
        "referenced_tools": _unique(referenced_tools),
        "object_count": object_count,
        "support_used": _to_bool(metadata.get("support_used", "false")),
        "is_seq_print": _to_bool(
            plate_json.get("is_seq_print", metadata.get("is_seq_print", "false"))
        ),
        "bed_type": plate_json.get("bed_type") or metadata.get("bed_type"),
        "nozzle_diameter": _to_float(
            plate_json.get("nozzle_diameter"), _to_float(_setting(settings, "nozzle_diameter"))
        ),
        "locked": _to_bool(plate_meta.get("locked", "false")),
        "printable": printable,
        "warnings": [
            {
                "message": str(warning.get("msg", "")),
                "level": _to_int(warning.get("level")),
                "code": warning.get("error_code") or None,
            }
            for warning in warnings
            if warning.get("msg")
        ],
        "nozzles": nozzles,
        "thumbnails": [],
    }


def parse_3mf(
    path: str | Path,
    extract_thumbnails: bool = True,
    thumb_dir: Optional[str | Path] = None,
) -> Dict[str, Any]:
    file_path = Path(path)
    zf, entries = _open_zip(file_path)
    try:
        model_settings_text = _read_text(
            zf, entries, "Metadata/model_settings.config"
        )
        slice_info_text = _read_text(zf, entries, "Metadata/slice_info.config")
        project_settings_text = _read_text(
            zf, entries, "Metadata/project_settings.config"
        )
        model_text = _read_text(zf, entries, "3D/3dmodel.model")

        model_settings: List[Dict[str, str]] = []
        if model_settings_text:
            try:
                model_settings = _parse_model_settings(model_settings_text)
            except ET.ParseError:
                model_settings = []
        slice_header: Dict[str, str] = {}
        slice_plates: List[Dict[str, Any]] = []
        if slice_info_text:
            try:
                slice_header, slice_plates = _parse_slice_info(slice_info_text)
            except ET.ParseError:
                slice_header, slice_plates = {}, []
        settings: Dict[str, Any] = {}
        if project_settings_text:
            try:
                parsed_settings = json.loads(project_settings_text)
                if isinstance(parsed_settings, dict):
                    settings = parsed_settings
            except json.JSONDecodeError:
                settings = {}
        model_metadata: Dict[str, str] = {}
        if model_text:
            try:
                model_metadata = _parse_model_metadata(model_text)
            except ET.ParseError:
                model_metadata = {}

        gcode_entries = _find_gcode_entries(entries)
        md5_entries = _find_md5_entries(entries)

        plate_meta_by_index: Dict[int, Dict[str, str]] = {}
        for meta in model_settings:
            index = _to_int(meta.get("plater_id"))
            if index is not None:
                plate_meta_by_index[index] = meta

        slice_by_index: Dict[int, Dict[str, Any]] = {}
        for slice_plate in slice_plates:
            index = _to_int(slice_plate["metadata"].get("index"))
            if index is not None:
                slice_by_index[index] = slice_plate

        all_indexes = sorted(
            set(plate_meta_by_index)
            | set(slice_by_index)
            | set(gcode_entries)
        )
        plates: List[Dict[str, Any]] = []
        for index in all_indexes:
            plate_meta = plate_meta_by_index.get(index, {})
            gcode_entry = gcode_entries.get(index)
            if gcode_entry is None:
                gcode_entry = plate_meta.get("gcode_file")
            gcode_info = _find_entry(entries, gcode_entry)
            if gcode_info is None:
                gcode_entry = None
            md5_entry = md5_entries.get(index)
            md5_value: Optional[str] = None
            if md5_entry:
                stored_md5 = _read_text(
                    zf, entries, md5_entry, MAX_MD5_ENTRY_SIZE
                )
                if stored_md5:
                    md5_value = stored_md5.strip()
            plate_json: Dict[str, Any] = {}
            pattern_json = _find_entry(
                entries, f"Metadata/plate_{index}.json"
            )
            if pattern_json is not None and pattern_json.file_size <= MAX_METADATA_ENTRY_SIZE:
                try:
                    plate_json = _parse_plate_json(
                        zf.read(pattern_json).decode("utf-8", errors="replace")
                    )
                except Exception:
                    plate_json = {}
            plate = _build_plate(
                index,
                plate_meta,
                slice_by_index.get(index),
                plate_json,
                gcode_entry,
                gcode_info,
                md5_entry,
                md5_value,
                settings,
            )
            plates.append(plate)

        all_thumbs: List[Dict[str, Any]] = []
        if extract_thumbnails and plates:
            thumb_path = (
                Path(thumb_dir)
                if thumb_dir is not None
                else file_path.parent.joinpath(".thumbs")
            )
            try:
                all_thumbs = _extract_thumbnails(
                    zf, entries, plates, thumb_path, safe_thumb_base(file_path.name)
                )
            except Exception:
                # Thumbnail extraction is best-effort.  Metadata and printing
                # must still work if the archive contains an unusual image.
                all_thumbs = []

        slicer = "Unknown"
        slicer_version: Optional[str] = None
        if slice_header.get("OrcaSlicer-Version"):
            slicer = "OrcaSlicer"
            slicer_version = slice_header["OrcaSlicer-Version"]
        elif slice_header.get("BambuStudio-Version"):
            slicer = "BambuStudio"
            slicer_version = slice_header["BambuStudio-Version"]
        elif model_metadata.get("Application", "").startswith("BambuStudio"):
            slicer = "BambuStudio"
            slicer_version = model_metadata["Application"].split("-", 1)[-1]
        elif model_metadata.get("OrcaSlicer"):
            slicer = "OrcaSlicer"
            slicer_version = model_metadata["OrcaSlicer"]

        printable_plates = [plate for plate in plates if plate["printable"]]
        estimated_time = sum(
            plate["prediction"] or 0.0 for plate in printable_plates
        )
        filament_weight_total = sum(
            plate["weight"] or 0.0 for plate in printable_plates
        )
        filament_total = sum(
            sum(
                (_to_float(filament.get("used_m")) or 0.0) * 1000.0
                for filament in (slice_by_index.get(plate["index"], {}).get("filaments", []))
            )
            for plate in printable_plates
        )
        filament_types = _unique(
            [
                value
                for plate in printable_plates
                for value in plate["filament_type"]
            ]
        )
        filament_colors = _unique(
            [
                value
                for plate in printable_plates
                for value in plate["filament_colors"]
            ]
        )
        filament_names = _unique(
            [
                value
                for plate in printable_plates
                for value in plate["filament_names"]
            ]
        )
        referenced_tools = _unique(
            [
                value
                for plate in printable_plates
                for value in plate["referenced_tools"]
            ]
        )
        nozzle_diameter = _to_float(_setting(settings, "nozzle_diameter"))
        layer_height = _to_float(_setting(settings, "layer_height"))
        first_layer_height = _to_float(
            _setting(settings, "initial_layer_print_height")
        )
        first_layer_extr_temp = _to_float(
            _first_setting(
                settings,
                "nozzle_temperature_initial_layer",
                "nozzle_temperature",
            )
        )
        first_layer_bed_temp = _bed_temp(settings)
        chamber_temp = _to_float(_setting(settings, "chamber_temperature"))

        metadata: Dict[str, Any] = {
            "size": file_path.stat().st_size,
            "modified": file_path.stat().st_mtime,
            "uuid": str(uuid.uuid4()),
            "file_processors": [],
            "slicer": slicer,
            "slicer_version": slicer_version or "?",
            "is_3mf": True,
            "sliced": bool(printable_plates),
            "plate_count": len(plates),
            "plates": plates,
            "thumbnails": all_thumbs,
            "estimated_time": round(estimated_time, 2),
            "filament_weight_total": round(filament_weight_total, 2),
            "filament_total": round(filament_total, 2),
            "filament_type": _collapse(filament_types),
            "filament_colors": filament_colors,
            "filament_name": _collapse(filament_names),
            "referenced_tools": referenced_tools,
            "nozzle_diameter": nozzle_diameter,
            "layer_height": layer_height,
            "first_layer_height": first_layer_height,
            "first_layer_extr_temp": first_layer_extr_temp,
            "first_layer_bed_temp": first_layer_bed_temp,
            "chamber_temp": chamber_temp,
            "printer_model": _first_setting(settings, "printer_model"),
            "printer_variant": _first_setting(settings, "printer_variant"),
            "bed_type": next(
                (plate["bed_type"] for plate in printable_plates if plate["bed_type"]),
                None,
            ),
            "print_start_time": None,
            "job_id": None,
        }
        return metadata
    finally:
        zf.close()


def get_plate_indexes(path: str | Path) -> List[int]:
    zf, entries = _open_zip(path)
    try:
        return sorted(_find_gcode_entries(entries))
    finally:
        zf.close()


def get_plate_info(path: str | Path, index: int) -> Dict[str, Any]:
    metadata = parse_3mf(path, extract_thumbnails=False)
    for plate in metadata["plates"]:
        if plate["index"] == index:
            return plate
    raise ThreeMfError(f"Plate {index} is not available in this project")


def extract_plate(
    path: str | Path,
    index: int,
    destination: str | Path,
    plate_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    src_path = Path(path)
    dest_path = Path(destination)
    if plate_info is None:
        plate_info = get_plate_info(src_path, index)
    gcode_entry = plate_info.get("gcode_filename")
    if not gcode_entry:
        raise ThreeMfError(f"Plate {index} does not contain sliced G-code")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(dest_path.name + ".tmp")
    zf, entries = _open_zip(src_path)
    try:
        info = _find_entry(entries, gcode_entry)
        if info is None:
            raise ThreeMfError(f"Missing archive entry: {gcode_entry}")
        md5 = hashlib.md5()
        with zf.open(info) as source, temp_path.open("wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                md5.update(chunk)
                target.write(chunk)
        computed_md5 = md5.hexdigest()
        expected_md5 = plate_info.get("gcode_md5")
        if expected_md5 and str(expected_md5).strip().lower() != computed_md5:
            temp_path.unlink(missing_ok=True)
            raise ThreeMfError(
                f"MD5 mismatch for plate {index}: "
                f"expected {expected_md5}, got {computed_md5}"
            )
        os.replace(temp_path, dest_path)
        return {
            "index": index,
            "name": plate_info.get("name", f"Plate {index}"),
            "gcode_filename": gcode_entry,
            "gcode_size": info.file_size,
            "gcode_md5": computed_md5,
            "path": str(dest_path),
        }
    finally:
        zf.close()
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
