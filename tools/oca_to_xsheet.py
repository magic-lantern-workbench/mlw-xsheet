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

"""Convert an Open Cel Animation (OCA) document (https://oca.rxlab.guide)
into an XSheet ExposureSheet XML document (xml/xsheet-core.xsd). It is the
counterpart of xsheet_to_oca.py.

The input is an OCA folder (``NAME.oca/``, holding the data file
``NAME.oca`` and optionally ``NAME_meta.json``) or the data file itself.
Only the JSON is read; the images are not needed.

An OCA document written by xsheet_to_oca.py carries the ExposureSheet data
OCA has no attributes for in its ``meta`` objects (``meta.xsheet``), and is
restored from them: the production IDs, assets, audio tracks, camera,
reviews, revision history and OTIO mapping, each frame's dialogue, notes
and audio cues, and each layer's assetRef, type and zOrder. Converting an
ExposureSheet to OCA and back gives the same exposure sheet.

Any other OCA document (from Krita, for example) is converted from what OCA
has, and the rest is filled in so the result is schema-valid:

- Production: Title from the document name, FrameRate from its frame rate
  (rounded to a whole number, as the schema requires), StartFrame and
  EndFrame from its timeline (``endTime`` is exclusive in OCA). ProjectID,
  SequenceID, SceneID and ShotID are ``UNKNOWN`` unless given on the command
  line. XSheet frames are numbered from 1, so a timeline starting at 0 (the
  OCA default) is shifted up by one; the header comment says so.
- Layers: each paint or vector layer becomes an XSheet layer, bottom to top
  as in OCA (zOrder 0, 10, 20, ...). Group layers are flattened into their
  child layers (a child named like another layer is renamed ``GROUP/NAME``).
  Clone and nested-OCA layers have no pictures of their own and are left
  out; the header comment lists them. Each layer gets an Asset (id from the
  layer name, category from its type) for its assetRef.
- Frames: an OCA frame shows its picture from ``frameNumber`` for
  ``duration`` frames; its ``name`` becomes the cel (or, for a name that
  looks like a 3D scene file such as ``.usd``, a 3D layer's sceneFile).
  ``_blank`` frames and gaps between frames leave the layer empty.
- Timeline: a <Frame> is written wherever some layer's exposure changes,
  listing the layers showing a picture there; a layer that goes empty is
  left out of that <Frame>, which is how an ExposureSheet marks it, and the
  frames between are holds. (A <Frame> needs at least one <Layer>, so if
  every layer is empty the bottom one is listed without a cel.) Notes are
  empty.
- VersionControl gets one Revision recording the conversion. AudioTracks,
  Camera, Reviews and OTIO are optional and left out.

Usage:
    python3 oca_to_xsheet.py INPUT.oca [-o OUTPUT.xml] [--project-id ID] [--sequence-id ID]
                             [--scene-id ID] [--shot-id ID] [--validate]
"""

from __future__ import annotations

import argparse
import datetime
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
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
SCHEMA_LOCATIONS = {
    "core": "xsheet-core.xsd",
    "asset": "xsheet-assets.xsd",
    "audio": "xsheet-audio.xsd",
    "cam": "xsheet-camera.xsd",
    "review": "xsheet-review.xsd",
    "otio": "xsheet-otio.xsd",
}
for _prefix, _uri in NS.items():
    ET.register_namespace("" if _prefix == "core" else _prefix, _uri)

BLANK = "_blank"
PICTURE_LAYERS = {"paintlayer", "vectorlayer"}
SCENE_FILE_RE = re.compile(r"\.(usd|usda|usdc|usdz|abc|fbx|ma|mb|obj|blend)$", re.IGNORECASE)
CORE_SCHEMA = Path(__file__).resolve().parent.parent / "xml" / "xsheet-core.xsd"
XSHEET_ORIGIN = "XSheet ExposureSheet XML"  # xsheet_to_oca.py's metadata originApp


class OCAConversionError(Exception):
    """Raised when an OCA document cannot be read or converted to XSheet XML."""


def qn(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def sub(parent, prefix: str, local: str, attrib: dict | None = None, text: str | None = None):
    el = ET.SubElement(parent, qn(prefix, local), {k: str(v) for k, v in (attrib or {}).items()})
    if text is not None:
        el.text = text
    return el


def to_ncname(value: str, used: set[str] | None = None) -> str:
    """Turn an arbitrary string into a valid, optionally deduplicated xs:ID (NCName)."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "ITEM")
    if not (name[0].isalpha() or name[0] == "_"):
        name = f"_{name}"
    if used is None:
        return name
    candidate, i = name, 2
    while candidate in used:
        candidate = f"{name}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def pick(values: dict, keys: tuple[str, ...]) -> dict:
    """The given keys of values, as strings, leaving out missing or empty ones."""
    return {k: str(values[k]) for k in keys if values.get(k) not in (None, "")}


# ---------------------------------------------------------------- reading OCA

def load_oca(path: Path) -> tuple[dict, dict, str]:
    """(OCA root object, metadata object or {}, document name) from an OCA
    folder or its data file."""
    if path.is_dir():
        stem = path.name[:-4] if path.name.endswith(".oca") else path.name
        candidates = [path / f"{stem}.oca", path / f"{stem}.json"]
        candidates += sorted(p for p in path.iterdir() if p.is_file() and p.suffix in (".oca", ".json")
                             and not p.name.endswith("_meta.json"))
        data_file = next((p for p in candidates if p.is_file()), None)
        if data_file is None:
            raise OCAConversionError(f"No OCA data file (.oca or .json) in {path}")
    elif path.is_file():
        data_file = path
    else:
        raise OCAConversionError(f"{path} does not exist")
    try:
        document = json.loads(data_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OCAConversionError(f"{data_file} is not an OCA JSON file: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("layers", []), list):
        raise OCAConversionError(f"{data_file} does not hold an OCA document object")
    meta_file = data_file.with_name(f"{data_file.stem}_meta.json")
    metadata = {}
    if meta_file.is_file():
        try:
            metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = {}  # OCA: use default values when the metadata can't be read
    return document, metadata if isinstance(metadata, dict) else {}, data_file.stem


def flatten_layers(layers: list[dict], group: str = "") -> tuple[list[tuple[str, dict]], list[str]]:
    """The picture layers bottom to top as (name, layer), with groups
    flattened, and the names of layers left out (clone / nested OCA)."""
    out: list[tuple[str, dict]] = []
    skipped: list[str] = []
    for layer in layers:
        name = str(layer.get("name") or "Layer")
        kind = layer.get("type", "paintlayer")
        if kind == "grouplayer":
            inner, inner_skipped = flatten_layers(layer.get("childLayers", []), f"{group}{name}/")
            out += inner
            skipped += inner_skipped
        elif kind in PICTURE_LAYERS:
            out.append((f"{group}{name}", layer))
        else:
            skipped.append(f"{group}{name} ({kind})")
    return out, skipped


def exposure(layer: dict, shift: int) -> dict[int, dict]:
    """XSheet frame number -> the OCA frame showing there (blank frames and gaps left out)."""
    shown: dict[int, dict] = {}
    for frame in layer.get("frames", []):
        if frame.get("name", BLANK) == BLANK or not frame.get("fileName", ""):
            continue
        try:
            start, length = int(frame["frameNumber"]), max(int(frame.get("duration", 1)), 1)
        except (KeyError, TypeError, ValueError):
            continue
        for number in range(start + shift, start + shift + length):
            shown[number] = frame
    return shown


# ---------------------------------------------------------------- building XSheet

def convert_document(document: dict, metadata: dict, *, source_name: str = "untitled",
                     ids: dict | None = None, xsheet_version: str | None = None) -> tuple[ET.Element, list[str]]:
    """Build an <ExposureSheet> from an OCA document and its metadata.
    Returns (root element, notes for the header comment)."""
    ids = ids or {}
    notes: list[str] = []
    saved = (metadata.get("meta") or {}).get("xsheet") or {}
    restoring = bool(saved) and metadata.get("originApp") == XSHEET_ORIGIN
    if restoring:
        notes.append("Restored from the ExposureSheet data xsheet_to_oca.py stored in the OCA metadata.")
    else:
        saved = {}

    layers, skipped = flatten_layers(document.get("layers", []))
    if skipped:
        notes.append("Left out (no pictures of their own): " + ", ".join(skipped) + ".")

    start_time = int(document.get("startTime", 0))
    end_time = int(document.get("endTime", 240))
    frame_numbers = [start_time, end_time - 1]
    for _name, layer in layers:
        for frame in layer.get("frames", []):
            if isinstance(frame.get("frameNumber"), int):
                frame_numbers.append(frame["frameNumber"])
    shift = max(0, 1 - min(frame_numbers))  # XSheet frame numbers start at 1
    if shift:
        notes.append(f"OCA frame numbers are shifted by +{shift} (XSheet frames start at 1): "
                     f"OCA frame {min(frame_numbers)} is XSheet frame {min(frame_numbers) + shift}.")
    start, end = start_time + shift, max(end_time - 1 + shift, start_time + shift)

    production = saved.get("production") or {}
    try:
        rate = float(document.get("frameRate", 24.0))
    except (TypeError, ValueError):
        rate = 24.0
    frame_rate = production.get("FrameRate") or str(max(1, round(rate)))
    if not production and rate != round(rate):
        notes.append(f"Frame rate {rate:g} is rounded to {frame_rate} (the schema needs a whole number).")

    root = ET.Element(qn("core", "ExposureSheet"), {
        "version": xsheet_version or (metadata.get("originAppVersion") if restoring else None) or "2.0",
        qn("xsi", "schemaLocation"): " ".join(f"{NS[p]} {SCHEMA_LOCATIONS[p]}" for p in SCHEMA_LOCATIONS),
    })
    prod_el = sub(root, "core", "Production")
    placeholders = []
    for field, key in (("ProjectID", "project_id"), ("SequenceID", "sequence_id"),
                       ("SceneID", "scene_id"), ("ShotID", "shot_id")):
        value = ids.get(key) or production.get(field)
        if not value:
            value = "UNKNOWN"
            placeholders.append(field)
        sub(prod_el, "core", field, text=value)
    sub(prod_el, "core", "Title", text=production.get("Title") or str(document.get("name") or source_name))
    sub(prod_el, "core", "FrameRate", text=str(frame_rate))
    sub(prod_el, "core", "StartFrame", text=str(start))
    sub(prod_el, "core", "EndFrame", text=str(end))
    if placeholders:
        notes.append("Placeholders (OCA has no equivalent): " + ", ".join(placeholders)
                     + " are UNKNOWN; the converter's project-id, sequence-id, scene-id and shot-id "
                       "options set them.")

    # ---- layers: names, attributes and exposure
    used_names: set[str] = set()
    used_ids: set[str] = set()
    layer_info = []  # (id, attrs without cel/sceneFile, {frame: value}, is 3D)
    assets = []
    for index, (path_name, layer) in enumerate(layers):
        name = path_name.rsplit("/", 1)[-1]
        if name in used_names or any(n.rsplit("/", 1)[-1] == name for n, _l in layers if n != path_name):
            name = path_name  # the same name elsewhere: keep its group path
        used_names.add(name)
        info = ((layer.get("meta") or {}).get("xsheet") or {}) if restoring else {}
        is_3d = info.get("type") == "3D"
        shown = exposure(layer, shift)
        values = {}
        for number, frame in shown.items():
            frame_info = ((frame.get("meta") or {}).get("xsheet") or {}) if restoring else {}
            value = frame_info.get("cel") or frame_info.get("sceneFile") or str(frame.get("name"))
            values[number] = value
            if not info and SCENE_FILE_RE.search(value):
                is_3d = True
        asset_ref = info.get("assetRef")
        if not asset_ref:
            asset_ref = to_ncname(name, used_ids)
            assets.append({"id": asset_ref, "name": name,
                           "category": "Vector" if layer.get("type") == "vectorlayer" else "Artwork",
                           "version": "1"})
        attrs = {"id": name, "assetRef": asset_ref, "type": "3D" if is_3d else "2D",
                 "zOrder": str(info.get("zOrder", index * 10))}
        layer_info.append((name, attrs, values, is_3d))

    # ---- Assets (required, at least one)
    assets_el = sub(root, "asset", "Assets")
    saved_assets = saved.get("assets") or []
    for asset in saved_assets:
        used_ids.add(asset.get("id", ""))
    for asset in [*saved_assets, *assets] or [{"id": to_ncname("UNKNOWN", used_ids), "name": "Unknown",
                                               "category": "Unknown", "version": "1"}]:
        sub(assets_el, "asset", "Asset", pick(asset, ("id", "name", "category", "version")))

    # ---- optional sections carried in the metadata
    tracks = saved.get("audioTracks") or []
    if tracks:
        tracks_el = sub(root, "audio", "AudioTracks")
        for track in tracks:
            sub(tracks_el, "audio", "Track", pick(track, ("id", "type", "file", "description")))
    camera = saved.get("camera") or {}
    if camera.get("keyframes"):  # a <Camera> needs at least one keyframe
        cam_el = sub(root, "cam", "Camera", pick(camera, ("cameraId", "name", "projection")))
        for move in camera.get("moves") or []:
            move_el = sub(cam_el, "cam", "CameraMove", pick(move, ("type", "startFrame", "endFrame")))
            if move.get("description"):
                sub(move_el, "cam", "Description", text=move["description"])
        for key in camera["keyframes"]:
            sub(cam_el, "cam", "Keyframe", pick(key, ("frame", "x", "y", "z", "rotationX", "rotationY", "rotationZ",
                                                      "zoom", "focalLength", "interpolation", "note")))

    # ---- Timeline: a <Frame> wherever an exposure changes, or notes/dialogue/audio sit
    dialogue = {int(d["frame"]): d for d in saved.get("dialogue") or [] if str(d.get("frame", "")).isdigit()}
    frame_notes = {int(n["frame"]): n.get("text", "") for n in saved.get("notes") or [] if str(n.get("frame", "")).isdigit()}
    cues: dict[int, list[dict]] = {}
    for cue in saved.get("audioCues") or []:
        if str(cue.get("frame", "")).isdigit():
            cues.setdefault(int(cue["frame"]), []).append(cue)
    keyframes = {start} | set(dialogue) | set(frame_notes) | set(cues)
    for _name, _attrs, values, _is_3d in layer_info:
        previous = None
        for number in range(start, end + 1):
            value = values.get(number)
            if value != previous:
                keyframes.add(number)
            previous = value
    layer_info.sort(key=lambda item: int(item[1]["zOrder"]) if str(item[1]["zOrder"]).lstrip("-").isdigit() else 0)
    timeline_el = sub(root, "core", "Timeline")
    for number in sorted(k for k in keyframes if start <= k <= end):
        frame_el = sub(timeline_el, "core", "Frame", {"number": number})
        layers_el = sub(frame_el, "core", "Layers")
        listed = 0
        for _name, attrs, values, is_3d in layer_info:
            value = values.get(number)
            if value is None:
                continue  # empty here: left out of the <Frame>
            sub(layers_el, "core", "Layer", {**{k: v for k, v in attrs.items() if k != "zOrder"},
                                             "sceneFile" if is_3d else "cel": value, "zOrder": attrs["zOrder"]})
            listed += 1
        if not listed:  # a <Frame> needs a <Layer>; with nothing showing, list one without a picture
            bottom = layer_info[0][1] if layer_info else {"id": "EMPTY", "assetRef": "UNKNOWN", "type": "2D", "zOrder": "0"}
            sub(layers_el, "core", "Layer", bottom)
        if number in dialogue:
            sub(frame_el, "core", "Dialogue", {"phoneme": dialogue[number].get("phoneme") or "rest"},
                text=dialogue[number].get("text", ""))
        for cue in cues.get(number, []):
            sub(frame_el, "core", "AudioRef", pick(cue, ("track", "startFrame", "endFrame")))
        sub(frame_el, "core", "Notes", text=frame_notes.get(number, ""))

    reviews = saved.get("reviews") or []
    if reviews:
        reviews_el = sub(root, "review", "Reviews")
        for review in reviews:
            review_el = sub(reviews_el, "review", "Review", pick(review, ("id", "frame", "reviewer", "status")))
            sub(review_el, "review", "Comment", text=review.get("comment", ""))

    vc_el = sub(root, "core", "VersionControl")
    revisions = saved.get("revisions") or []
    for revision in revisions:
        sub(vc_el, "core", "Revision", pick(revision, ("number", "author", "date")), text=revision.get("text", ""))
    if not revisions:
        sub(vc_el, "core", "Revision", {"number": 1, "author": "oca_to_xsheet.py",
                                        "date": datetime.date.today().isoformat()},
            text=f"Converted from the OCA document {document.get('name') or source_name}.")

    otio = saved.get("otio") or {}
    if otio.get("timelineRef"):
        otio_el = sub(root, "otio", "OTIO")
        sub(otio_el, "otio", "TimelineRef", text=otio["timelineRef"])
        for track_map in otio.get("trackMaps") or []:
            sub(otio_el, "otio", "TrackMap", pick(track_map, ("xsheetLayer", "otioTrack")))
    return root, notes


def render(root: ET.Element, notes: list[str], source: str) -> str:
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    lines = [f"Auto-generated by oca_to_xsheet.py from {source}.", *notes]
    header = "\n".join(f"<!-- {line.replace('--', '- -')} -->" for line in lines)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n\n{header}\n\n{body}\n'


def convert_path(path: Path, **kwargs) -> str:
    """Convert an OCA folder or data file into ExposureSheet XML text."""
    document, metadata, stem = load_oca(path)
    root, notes = convert_document(document, metadata, source_name=stem, **kwargs)
    return render(root, notes, path.name)


def validate(xml_text: str) -> list[str]:
    """Schema errors for the ExposureSheet (empty if valid), using xmlschema."""
    import xmlschema
    schema = xmlschema.XMLSchema(str(CORE_SCHEMA))
    return [f"{err.path}: {err.reason}" for err in schema.iter_errors(xml_text)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="OCA folder (NAME.oca/) or its data file")
    parser.add_argument("-o", "--output", type=Path, help="Output XML path (default: NAME.xml next to the input)")
    parser.add_argument("--project-id", help="Production/ProjectID (not in OCA)")
    parser.add_argument("--sequence-id", help="Production/SequenceID (not in OCA)")
    parser.add_argument("--scene-id", help="Production/SceneID (not in OCA)")
    parser.add_argument("--shot-id", help="Production/ShotID (not in OCA)")
    parser.add_argument("--xsheet-version", help="ExposureSheet/@version (default 2.0, or the original's)")
    parser.add_argument("--validate", action="store_true", help="Validate the result against xml/xsheet-core.xsd")
    parser.add_argument("--force", action="store_true", help="Replace the output file if it already exists")
    args = parser.parse_args()

    ids = {k: getattr(args, k) for k in ("project_id", "sequence_id", "scene_id", "shot_id") if getattr(args, k)}
    try:
        xml_text = convert_path(args.input, ids=ids, xsheet_version=args.xsheet_version)
    except OCAConversionError as exc:
        print(f"Could not convert {args.input}: {exc}", file=sys.stderr)
        return 1

    source = args.input.resolve()
    stem = source.name[:-4] if source.name.endswith(".oca") else source.stem
    output = args.output or source.parent / f"{stem}.xml"
    if output.exists() and not args.force:
        print(f"{output} already exists; use --force to replace it", file=sys.stderr)
        return 1
    output.write_text(xml_text, encoding="utf-8")

    if args.validate:
        errors = validate(xml_text)
        if errors:
            print(f"Wrote {output}, but it is not schema-valid:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
    print(f"Wrote {output}" + (" (valid against xsheet-core.xsd)" if args.validate else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
