#!/usr/bin/env python3
"""Convert an XSheet XML ExposureSheet document (xml/xsheet-*.xsd) into an
XDTS JSON timesheet (tools/xdts-20261206.json).

This module is used two ways:

- As a standalone CLI tool (see ``main()`` / ``if __name__ == "__main__"``
  below).
- As a library imported by the Editor (main.py) to power an "Export XDTS
  JSON" feature, via ``convert_string()``/``convert_element()`` (which work
  directly off in-editor text, no file on disk required) and
  ``export_xdts_json()`` (which also renders/validates the JSON).

The two formats model exposure sheets very differently, so this converter
makes a few explicit mapping decisions:

- header.cut / header.scene must be short digit strings (schema pattern
  ``\\d{1,4}``), while XSheet uses free-form IDs like "SH005" / "SC010".
  The trailing digits of ShotID/SceneID are used, right-truncated to 4.
- timeTables[].fields[fieldId=0] ("Cell") gets one track per XSheet <Layer>,
  ordered by ascending zOrder (trackNo 0 = bottom, per the XDTS spec). Only
  cel *changes* are emitted (XDTS holds a cell's value until the next
  entry), and a layer dropping out of a later <Frame> emits a single
  SYMBOL_NULL_CELL entry rather than repeating it every frame.
- fieldId=3 ("Dialog") gets a single track. XDTS's two value slots are
  documented as [speakerName, dialogText]; XSheet has no speaker field, so
  they are repurposed as [phoneme, dialogText] to avoid losing the lipsync
  code. This is a deliberate reinterpretation, not a spec-sanctioned use.
- fieldId=5 ("Camerawork") gets a single track built from <CameraMove>
  descriptions plus any <Keyframe note="..."> cues. XDTS's Camerawork cell
  is a single instruction string, so it has no home for the numeric
  <Keyframe> position/rotation/zoom animation data -- that is intentionally
  dropped.
- AudioRef, Notes, Reviews and VersionControl have no XDTS equivalent and
  are dropped.

Usage:
    python3 xsheet_to_xdts.py INPUT.xml [-o OUTPUT.json] [--xdts-version {5,10}] [--validate]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "core": "http://schemas.animation.org/xsheet/core",
    "asset": "http://schemas.animation.org/xsheet/assets",
    "audio": "http://schemas.animation.org/xsheet/audio",
    "cam": "http://schemas.animation.org/xsheet/camera",
    "review": "http://schemas.animation.org/xsheet/review",
    "otio": "http://schemas.animation.org/xsheet/otio",
}

FIELD_CELL = 0
FIELD_DIALOG = 3
FIELD_CAMERAWORK = 5

SYMBOL_NULL_CELL = "SYMBOL_NULL_CELL"

DEFAULT_SCHEMA_PATH = Path(__file__).with_name("xdts-20261206.json")


class XSheetConversionError(Exception):
    """Raised when an XSheet document cannot be parsed or converted to XDTS."""


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def text_of(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def digits_for_pattern(value: str, fallback: str = "0") -> str:
    """Extract up to 4 trailing digits to satisfy the \\d{1,4} header pattern."""
    match = re.findall(r"\d+", value or "")
    if not match:
        return fallback
    digits = "".join(match)
    return digits[-4:] if len(digits) > 4 else digits


def parse_production(root) -> dict:
    prod = root.find(q("core", "Production"))
    if prod is None:
        return {}
    fields = ("ProjectID", "SequenceID", "SceneID", "ShotID", "Title",
              "FrameRate", "StartFrame", "EndFrame")
    out = {}
    for name in fields:
        el = prod.find(q("core", name))
        out[name] = text_of(el) if el is not None else None
    return out


def parse_frames(root) -> list[dict]:
    timeline = root.find(q("core", "Timeline"))
    if timeline is None:
        return []
    frames = []
    for frame_el in timeline.findall(q("core", "Frame")):
        number = int(frame_el.get("number"))
        layers = []
        layers_el = frame_el.find(q("core", "Layers"))
        if layers_el is not None:
            for layer_el in layers_el.findall(q("core", "Layer")):
                layers.append({
                    "id": layer_el.get("id"),
                    "zOrder": int(layer_el.get("zOrder", "0")),
                    "cel": layer_el.get("cel") or layer_el.get("sceneFile"),
                })
        dialogue_el = frame_el.find(q("core", "Dialogue"))
        dialogue = None
        if dialogue_el is not None:
            dialogue = {
                "phoneme": dialogue_el.get("phoneme"),
                "text": text_of(dialogue_el),
            }
        frames.append({"number": number, "layers": layers, "dialogue": dialogue})
    frames.sort(key=lambda f: f["number"])
    return frames


def parse_camera(root) -> dict:
    camera_el = root.find(q("cam", "Camera"))
    if camera_el is None:
        return {}
    name = camera_el.get("name") or camera_el.get("cameraId") or "Camera"
    moves = []
    for move_el in camera_el.findall(q("cam", "CameraMove")):
        desc_el = move_el.find(q("cam", "Description"))
        moves.append({
            "type": move_el.get("type"),
            "startFrame": int(move_el.get("startFrame")),
            "endFrame": int(move_el.get("endFrame")),
            "description": text_of(desc_el),
        })
    keyframe_notes = []
    for kf_el in camera_el.findall(q("cam", "Keyframe")):
        note = kf_el.get("note")
        if note:
            keyframe_notes.append({"frame": int(kf_el.get("frame")), "note": note})
    return {"name": name, "moves": moves, "keyframeNotes": keyframe_notes}


def build_cell_field(frames: list[dict], start_frame: int) -> tuple[dict, list[str]]:
    zorder_by_layer: dict[str, int] = {}
    for frame in frames:
        for layer in frame["layers"]:
            zorder_by_layer.setdefault(layer["id"], layer["zOrder"])

    layer_ids = sorted(zorder_by_layer, key=lambda lid: zorder_by_layer[lid])
    track_no = {lid: i for i, lid in enumerate(layer_ids)}

    track_frames: dict[str, list[dict]] = {lid: [] for lid in layer_ids}
    last_value: dict[str, str | None] = {lid: None for lid in layer_ids}

    for frame in frames:
        present = {layer["id"]: layer["cel"] for layer in frame["layers"] if layer["cel"]}
        idx = frame["number"] - start_frame

        for lid in layer_ids:
            if lid in present:
                value = present[lid]
                if value != last_value[lid]:
                    track_frames[lid].append({
                        "frame": idx,
                        "data": [{"id": 0, "values": [value]}],
                    })
                    last_value[lid] = value
            elif last_value[lid] not in (None, SYMBOL_NULL_CELL):
                track_frames[lid].append({
                    "frame": idx,
                    "data": [{"id": 0, "values": [SYMBOL_NULL_CELL]}],
                })
                last_value[lid] = SYMBOL_NULL_CELL

    tracks = [
        {"trackNo": track_no[lid], "frames": track_frames[lid]}
        for lid in layer_ids
        if track_frames[lid]
    ]
    return {"fieldId": FIELD_CELL, "tracks": tracks}, layer_ids


def build_dialog_field(frames: list[dict], start_frame: int) -> dict:
    entries = []
    for frame in frames:
        dialogue = frame["dialogue"]
        if not dialogue:
            continue
        values = [v for v in (dialogue["phoneme"], dialogue["text"]) if v]
        if not values:
            continue
        entries.append({
            "frame": frame["number"] - start_frame,
            "data": [{"id": 0, "values": values}],
        })
    if not entries:
        return None
    return {"fieldId": FIELD_DIALOG, "tracks": [{"trackNo": 0, "frames": entries}]}


def build_camerawork_field(camera: dict, start_frame: int) -> dict:
    if not camera:
        return None
    by_frame: dict[int, list[str]] = {}
    for move in camera["moves"]:
        idx = move["startFrame"] - start_frame
        instruction = f"{move['type']}: {move['description']}" if move["description"] else move["type"]
        by_frame.setdefault(idx, []).append(instruction)
    for kf in camera["keyframeNotes"]:
        idx = kf["frame"] - start_frame
        by_frame.setdefault(idx, []).append(kf["note"])

    if not by_frame:
        return None

    entries = [
        {"frame": idx, "data": [{"id": 0, "values": [" / ".join(by_frame[idx])]}]}
        for idx in sorted(by_frame)
    ]
    return {"fieldId": FIELD_CAMERAWORK, "tracks": [{"trackNo": 0, "frames": entries}]}


def convert_element(root: ET.Element, xdts_version: int = 10, *, source_name: str = "untitled") -> dict:
    """Convert an already-parsed XSheet <ExposureSheet> root element into an
    XDTS dict. This is the core conversion shared by every entry point below.

    :param source_name: fallback timeTable name used only when the document
        has no Production/Title, SequenceID, SceneID or ShotID to build one
        from (e.g. the source file's stem, or "untitled" for in-editor text
        that was never saved).
    """
    production = parse_production(root)
    frames = parse_frames(root)
    camera = parse_camera(root)

    start_frame = int(production.get("StartFrame") or 1)
    end_frame = production.get("EndFrame")
    max_frame_seen = max((f["number"] for f in frames), default=start_frame)
    end_frame = int(end_frame) if end_frame else max_frame_seen
    duration = max(end_frame, max_frame_seen) - start_frame + 1

    cell_field, layer_ids = build_cell_field(frames, start_frame)
    dialog_field = build_dialog_field(frames, start_frame)
    camerawork_field = build_camerawork_field(camera, start_frame)

    fields = [cell_field]
    headers = [{"fieldId": FIELD_CELL, "names": layer_ids}]

    if dialog_field:
        fields.append(dialog_field)
        headers.append({"fieldId": FIELD_DIALOG, "names": ["Dialogue"]})

    if camerawork_field:
        fields.append(camerawork_field)
        headers.append({"fieldId": FIELD_CAMERAWORK, "names": [camera.get("name", "Camera")]})

    name = production.get("Title") or " ".join(
        v for v in (production.get("SequenceID"), production.get("SceneID"), production.get("ShotID")) if v
    ) or source_name

    cut = digits_for_pattern(production.get("ShotID") or "")
    scene = digits_for_pattern(production.get("SceneID") or "")

    return {
        "header": {"cut": cut, "scene": scene},
        "timeTables": [{
            "name": name,
            "duration": duration,
            "fields": fields,
            "timeTableHeaders": headers,
        }],
        "version": xdts_version,
    }


def convert_string(xml_text: str, xdts_version: int = 10, *, source_name: str = "untitled") -> dict:
    """Convert XSheet XML held as a string (e.g. an editor buffer that may not
    be saved to disk) into an XDTS dict."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    return convert_element(root, xdts_version, source_name=source_name)


def convert_file(xml_path: Path, xdts_version: int = 10) -> dict:
    """Convert an XSheet XML file on disk into an XDTS dict."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    return convert_element(tree.getroot(), xdts_version, source_name=xml_path.stem)


def validate_against_schema(data: dict, schema_path: Path | None = None) -> None:
    """Validate an XDTS dict against the XDTS JSON schema, raising on failure
    (jsonschema.ValidationError, or another exception if the schema itself
    can't be loaded)."""
    import jsonschema

    schema_path = schema_path or DEFAULT_SCHEMA_PATH
    with schema_path.open(encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(instance=data, schema=schema)


def export_xdts_json(
    xml_text: str,
    *,
    xdts_version: int = 10,
    validate: bool = False,
    indent: int | None = 2,
    source_name: str = "untitled",
) -> str:
    """High-level entry point for the Editor's Export feature: convert XSheet
    XML text straight to a rendered XDTS JSON string.

    Raises XSheetConversionError if the XML can't be parsed, or
    jsonschema.ValidationError if ``validate`` is set and the result doesn't
    satisfy the XDTS schema. Callers (e.g. main.py) are expected to catch
    these and report them to the user rather than let them propagate.
    """
    data = convert_string(xml_text, xdts_version, source_name=source_name)
    if validate:
        validate_against_schema(data)
    return json.dumps(data, indent=indent) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="XSheet ExposureSheet XML file")
    parser.add_argument("-o", "--output", type=Path, help="Output XDTS JSON path (default: alongside input)")
    parser.add_argument("--xdts-version", type=int, choices=(5, 10), default=10, help="XDTS 'version' field to emit")
    parser.add_argument("--validate", action="store_true", help="Validate the result against tools/xdts-20261206.json")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact)")
    args = parser.parse_args()

    try:
        data = convert_file(args.input, args.xdts_version)
    except XSheetConversionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.validate:
        try:
            validate_against_schema(data)
        except Exception as exc:  # jsonschema.ValidationError or similar
            print(f"XDTS validation failed: {exc}", file=sys.stderr)
            return 1

    output_path = args.output or args.input.with_suffix(".xdts.json")
    indent = args.indent or None
    output_path.write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
