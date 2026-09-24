#!/usr/bin/env python3
# COPYRIGHT_BEGIN
#
# MIT License
#
# Copyright (c) 2026 Wizzer Works
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# COPYRIGHT_END

"""Convert an XSheet XML ExposureSheet document (xml/xsheet-*.xsd) into an
XDTS-Extended JSON timesheet (tools/xdts-extended.schema.json).

This is the extended-schema counterpart of xsheet_to_xdts.py, which targets
the older tools/xdts-20261206.json fieldId/track/value format. The original
module is left untouched; this is a new, independent converter that happens
to keep the same module shape so it can be dropped into the Editor's
"Export XDTS JSON" feature (main.py) in place of the legacy one -- it
exposes the same ``convert_string()`` / ``convert_element()`` / ``convert_file()``
/ ``export_xdts_json()`` / ``validate_against_schema()`` functions, with the
same call signatures main.py already uses (``export_xdts_json(text,
source_name=source_name)``), minus the legacy module's ``xdts_version``
parameter, which has no equivalent in the extended schema (it carries no
format-version field at all).

This module is used two ways:

- As a standalone CLI tool (see ``main()`` / ``if __name__ == "__main__"``
  below).
- As a library imported by the Editor (main.py) to power the "Export XDTS
  JSON" feature, via ``convert_string()``/``convert_element()`` (which work
  directly off in-editor text, no file on disk required) and
  ``export_xdts_json()`` (which also renders/validates the JSON).

Since the extended schema is a much closer structural match to ExposureSheet
than the legacy fieldId/track format, this is the reverse of
xdts_extended_to_xsheet.py's mapping, not a reinterpretation of it:

- Every <Layer> that appears anywhere in the Timeline becomes one entry in
  `layers`, ordered by ascending @zOrder (index 0 = bottom). XSheet's
  Timeline only records "trigger" frames (a frame is only present when
  something changes), so a layer's cel is treated as held from the frame it
  first appears/changes until the next Frame where it changes or drops out;
  each such run becomes one `{frame, cell, exposure}` entry. A layer that is
  still present at Production/EndFrame gets one final run closed at that
  frame. The synthesized "_placeholder"-prefixed Layer that
  xdts_extended_to_xsheet.py inserts into otherwise-empty frames (cel
  "UNKNOWN") is recognized and dropped -- it exists only to satisfy the
  ExposureSheet schema's "Layers must have >=1 Layer" rule and carries no
  real data.
- Each layer's `type` (character/background/effects) is read back from the
  matching <asset:Asset>'s @category (matched via @assetRef), lower-cased
  and validated against the extended enum; anything else (including the
  legacy converter's "Unknown" placeholder) falls back to "character".
- <Frame>/<Notes> text becomes `marks`: the text is split on "; " (the
  delimiter xdts_extended_to_xsheet.py joins multiple marks with) and each
  part is read as "TYPE: note" when it contains ": ", else as a bare TYPE
  with no note. An empty Notes string produces no mark for that frame. This
  is a heuristic that losslessly reverses xdts_extended_to_xsheet.py's own
  formatting, not a general free-text parser.
- <cam:Camera>/<cam:Keyframe> elements become `camera` entries: x/y/z ->
  position.{x,y,z} (position is only emitted when both x and y are present,
  matching the extended schema's requirement that position.x/y are
  required together), zoom -> zoom, rotationZ -> rotation, rotationX ->
  tilt. A single Keyframe carrying the
  "Synthesized placeholder - ..." @note that xdts_extended_to_xsheet.py
  writes when the source had no camera data is recognized and produces an
  empty `camera` list rather than a bogus entry. <CameraMove> elements and
  any other Keyframe attribute (rotationY, focalLength, interpolation,
  a non-placeholder note) have no home in the extended schema and are
  dropped.
- <AudioRef> elements become `audio` entries, with the referenced
  <audio:Track>'s @file supplying `file` and (endFrame - startFrame + 1)
  supplying `durationFrames`. The synthesized "AUDIO_PLACEHOLDER" track
  that xdts_extended_to_xsheet.py writes when the source had no audio data
  is never referenced by an AudioRef, so it is naturally skipped. Track
  @type (Dialogue/Music/Effects) and Audio's optional `description` have
  no reverse mapping and are dropped.
- <Dialogue> has no equivalent in the extended schema at all (unlike the
  legacy fieldId=3 "Dialog" field) and is always dropped.
- Production/SceneID and Production/ShotID map directly to `scene`/`cut`
  (the extended schema takes free-form strings, unlike the legacy format's
  \\d{1,4}-only header fields, so no digit-truncation is needed).
  Production/FrameRate maps directly to `frameRate`. ProjectID, SequenceID
  and Title have no home in the extended schema and are dropped.

Usage:
    python3 xsheet_to_xdts_extended.py INPUT.xml [-o OUTPUT.json] [--validate]
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
}

LAYER_TYPE_ENUM = ("character", "background", "effects")
PLACEHOLDER_LAYER_PREFIX = "_placeholder"
PLACEHOLDER_CAMERA_NOTE_PREFIX = "Synthesized placeholder"

DEFAULT_SCHEMA_PATH = Path(__file__).with_name("xdts-extended.schema.json")


class XSheetConversionError(Exception):
    """Raised when an XSheet document cannot be parsed or converted to XDTS-Extended."""


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def text_of(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _num(value: str):
    """Parse an XSD xs:decimal string into an int or float, whichever is exact."""
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return float(value)


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


def parse_assets(root) -> dict[str, dict]:
    assets_el = root.find(q("asset", "Assets"))
    result: dict[str, dict] = {}
    if assets_el is None:
        return result
    for asset_el in assets_el.findall(q("asset", "Asset")):
        result[asset_el.get("id")] = {
            "name": asset_el.get("name"),
            "category": asset_el.get("category"),
        }
    return result


def parse_audio_tracks(root) -> dict[str, str]:
    tracks_el = root.find(q("audio", "AudioTracks"))
    result: dict[str, str] = {}
    if tracks_el is None:
        return result
    for track_el in tracks_el.findall(q("audio", "Track")):
        result[track_el.get("id")] = track_el.get("file")
    return result


def is_placeholder_layer(layer_el) -> bool:
    layer_id = layer_el.get("id") or ""
    return layer_id.startswith(PLACEHOLDER_LAYER_PREFIX) and layer_el.get("cel") == "UNKNOWN"


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
                if is_placeholder_layer(layer_el):
                    continue
                layers.append({
                    "id": layer_el.get("id"),
                    "assetRef": layer_el.get("assetRef"),
                    "zOrder": int(layer_el.get("zOrder", "0")),
                    "cel": layer_el.get("cel") or layer_el.get("sceneFile"),
                })
        notes = text_of(frame_el.find(q("core", "Notes")))
        audio_refs = [
            {
                "track": ref_el.get("track"),
                "startFrame": int(ref_el.get("startFrame")),
                "endFrame": int(ref_el.get("endFrame")),
            }
            for ref_el in frame_el.findall(q("core", "AudioRef"))
        ]
        frames.append({"number": number, "layers": layers, "notes": notes, "audioRefs": audio_refs})
    frames.sort(key=lambda f: f["number"])
    return frames


def build_layers(frames: list[dict], assets_by_id: dict[str, dict], end_frame: int) -> list[dict]:
    zorder_by_layer: dict[str, int] = {}
    name_by_layer: dict[str, str] = {}
    category_by_layer: dict[str, str] = {}

    for frame in frames:
        for layer in frame["layers"]:
            lid = layer["id"]
            zorder_by_layer.setdefault(lid, layer["zOrder"])
            if lid not in name_by_layer:
                asset = assets_by_id.get(layer["assetRef"]) or {}
                name_by_layer[lid] = asset.get("name") or lid
                category = (asset.get("category") or "").lower()
                category_by_layer[lid] = category if category in LAYER_TYPE_ENUM else "character"

    layer_ids = sorted(zorder_by_layer, key=lambda lid: zorder_by_layer[lid])

    result = []
    for lid in layer_ids:
        runs = []
        current_start = None
        current_cel = None
        for frame in frames:
            present = next((l for l in frame["layers"] if l["id"] == lid), None)
            if present is not None:
                cel = present["cel"]
                if current_start is None or cel != current_cel:
                    if current_start is not None:
                        runs.append({
                            "frame": current_start, "cell": current_cel,
                            "exposure": frame["number"] - current_start,
                        })
                    current_start, current_cel = frame["number"], cel
            elif current_start is not None:
                runs.append({
                    "frame": current_start, "cell": current_cel,
                    "exposure": frame["number"] - current_start,
                })
                current_start, current_cel = None, None

        if current_start is not None:
            runs.append({
                "frame": current_start, "cell": current_cel,
                "exposure": end_frame - current_start + 1,
            })

        result.append({"name": name_by_layer[lid], "type": category_by_layer[lid], "frames": runs})

    return result


def build_marks(frames: list[dict]) -> list[dict]:
    marks = []
    for frame in frames:
        notes = frame["notes"]
        if not notes:
            continue
        for part in notes.split("; "):
            part = part.strip()
            if not part:
                continue
            if ": " in part:
                mark_type, note = part.split(": ", 1)
                marks.append({"frame": frame["number"], "type": mark_type, "note": note})
            else:
                marks.append({"frame": frame["number"], "type": part})
    return marks


def build_camera(root) -> list[dict]:
    camera_el = root.find(q("cam", "Camera"))
    if camera_el is None:
        return []

    keyframes = camera_el.findall(q("cam", "Keyframe"))
    if len(keyframes) == 1:
        note = keyframes[0].get("note") or ""
        if note.startswith(PLACEHOLDER_CAMERA_NOTE_PREFIX):
            return []

    entries = []
    for kf in keyframes:
        entry = {"frame": int(kf.get("frame"))}
        x, y, z = kf.get("x"), kf.get("y"), kf.get("z")
        if x is not None and y is not None:
            position = {"x": _num(x), "y": _num(y)}
            if z is not None:
                position["z"] = _num(z)
            entry["position"] = position
        if kf.get("zoom") is not None:
            entry["zoom"] = _num(kf.get("zoom"))
        if kf.get("rotationZ") is not None:
            entry["rotation"] = _num(kf.get("rotationZ"))
        if kf.get("rotationX") is not None:
            entry["tilt"] = _num(kf.get("rotationX"))
        entries.append(entry)

    entries.sort(key=lambda e: e["frame"])
    return entries


def build_audio(frames: list[dict], audio_tracks: dict[str, str]) -> list[dict]:
    entries = []
    for frame in frames:
        for ref in frame["audioRefs"]:
            file_name = audio_tracks.get(ref["track"])
            if not file_name:
                continue
            entries.append({
                "startFrame": ref["startFrame"],
                "durationFrames": ref["endFrame"] - ref["startFrame"] + 1,
                "file": file_name,
            })
    entries.sort(key=lambda a: a["startFrame"])
    return entries


def convert_element(root: ET.Element, *, source_name: str = "untitled") -> dict:
    """Convert an already-parsed XSheet <ExposureSheet> root element into an
    XDTS-Extended dict. This is the core conversion shared by every entry
    point below.

    :param source_name: accepted for call-signature compatibility with
        main.py's Export XDTS JSON feature (which always passes it). The
        extended schema has no title/name field, so it is currently unused.
    """
    production = parse_production(root)
    assets_by_id = parse_assets(root)
    audio_tracks = parse_audio_tracks(root)
    frames = parse_frames(root)

    max_frame_seen = max((f["number"] for f in frames), default=1)
    end_frame = int(production.get("EndFrame") or max_frame_seen)

    data = {
        "scene": production.get("SceneID") or "UNKNOWN",
        "cut": production.get("ShotID") or "UNKNOWN",
        "frameRate": int(production.get("FrameRate") or 24),
        "layers": build_layers(frames, assets_by_id, end_frame),
    }

    marks = build_marks(frames)
    if marks:
        data["marks"] = marks

    camera = build_camera(root)
    if camera:
        data["camera"] = camera

    audio = build_audio(frames, audio_tracks)
    if audio:
        data["audio"] = audio

    return data


def convert_string(xml_text: str, *, source_name: str = "untitled") -> dict:
    """Convert XSheet XML held as a string (e.g. an editor buffer that may not
    be saved to disk) into an XDTS-Extended dict."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    return convert_element(root, source_name=source_name)


def convert_file(xml_path: Path) -> dict:
    """Convert an XSheet XML file on disk into an XDTS-Extended dict."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    return convert_element(tree.getroot(), source_name=xml_path.stem)


def validate_against_schema(data: dict, schema_path: Path | None = None) -> None:
    """Validate an XDTS-Extended dict against its JSON schema, raising on
    failure (jsonschema.ValidationError, or another exception if the schema
    itself can't be loaded)."""
    import jsonschema

    schema_path = schema_path or DEFAULT_SCHEMA_PATH
    with schema_path.open(encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(instance=data, schema=schema)


def export_xdts_json(
    xml_text: str,
    *,
    validate: bool = False,
    indent: int | None = 2,
    source_name: str = "untitled",
) -> str:
    """High-level entry point for the Editor's Export feature: convert XSheet
    XML text straight to a rendered XDTS-Extended JSON string.

    Raises XSheetConversionError if the XML can't be parsed, or
    jsonschema.ValidationError if ``validate`` is set and the result doesn't
    satisfy the XDTS-Extended schema. Callers (e.g. main.py) are expected to
    catch these and report them to the user rather than let them propagate.
    """
    data = convert_string(xml_text, source_name=source_name)
    if validate:
        validate_against_schema(data)
    return json.dumps(data, indent=indent) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="XSheet ExposureSheet XML file")
    parser.add_argument("-o", "--output", type=Path, help="Output XDTS-Extended JSON path (default: alongside input)")
    parser.add_argument("--validate", action="store_true", help="Validate the result against tools/xdts-extended.schema.json")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact)")
    args = parser.parse_args()

    try:
        data = convert_file(args.input)
    except XSheetConversionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.validate:
        try:
            validate_against_schema(data)
        except Exception as exc:  # jsonschema.ValidationError or similar
            print(f"XDTS-Extended validation failed: {exc}", file=sys.stderr)
            return 1

    output_path = args.output or args.input.with_suffix(".xdts.json")
    indent = args.indent or None
    output_path.write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
