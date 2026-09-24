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

"""Convert an XDTS-Extended JSON timesheet (tools/xdts-extended.schema.json)
into an XSheet ExposureSheet XML document (xml/xsheet-core.xsd).

This is the extended-schema counterpart of xdts_to_xsheet.py, which targets
the older tools/xdts-20261206.json fieldId/track/value format. The two
source formats are structurally unrelated -- this one is a flat, per-layer
list of exposure runs with native camera/audio/mark data, not
fieldId/track/value tuples -- so this is a new converter rather than a
patch to the original, which is left untouched.

The extended schema is a much closer match to ExposureSheet than the legacy
format:

- Each entry in `layers` becomes one <Layer> per track, ordered by array
  position (index 0 = bottom, per zOrder). A layer's `frames` array is a
  list of exposure runs (`{frame, cell, exposure}`); each run holds its
  `cell` value from `frame` through `frame + exposure - 1`. A gap between
  one run's end and the next run's start (or the end of the sheet) makes
  the layer absent from the Timeline for those frames. `layers[].type`
  (character/background/effects) becomes the synthesized Asset's
  @category. A cel value that looks like a 3D scene file (.usd/.abc/.fbx/
  ...) is reconstructed as a 3D layer using @sceneFile instead of @cel.
- `camera` entries carry genuine numeric data and map directly to
  <cam:Keyframe> attributes: position.{x,y,z} -> x/y/z, zoom -> zoom,
  rotation -> rotationZ, tilt -> rotationX. (The extended schema doesn't
  fix an axis convention for rotation/tilt; rotationZ/rotationX is a
  reasonable reading of "in-plane rotation" vs. "tilt".) No <CameraMove>
  elements are synthesized since the source has no move-type/description
  text, only keyframes.
- `audio` entries become real <audio:Track> elements (not placeholders)
  plus one <AudioRef> on the Timeline Frame where each clip starts.
  XDTS-Extended has no track-type field, so Dialogue/Music/Effects is
  guessed from the file name / description (falls back to Effects).
- `marks` entries become that frame's <Notes> text ("TYPE: note", joined
  with "; " if several marks share a frame).
- The extended schema has no dialogue/phoneme concept at all, so no
  <Dialogue> elements are ever emitted (it's optional in the schema).
- ProjectID, SequenceID, Reviews, VersionControl and OTIO still have no
  XDTS-Extended equivalent and are filled with placeholders (overridable
  via CLI flags where it makes sense). Unlike the legacy converter,
  FrameRate, SceneID/ShotID (from scene/cut) and the Camera's keyframes
  now come from real source data instead of being synthesized whenever
  the source file provides them.

Usage:
    python3 xdts_extended_to_xsheet.py INPUT.xdts.json [-o OUTPUT.xml] [--validate]
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import json
import re
import shutil
import subprocess
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

DEFAULT_INPUT_SCHEMA_PATH = Path(__file__).with_name("xdts-extended.schema.json")

SCENE_FILE_RE = re.compile(r"\.(usd|usda|usdc|usdz|abc|fbx|ma|mb|obj|blend)$", re.IGNORECASE)


def qn(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def sub(parent, prefix: str, local: str, attrib: dict | None = None, text: str | None = None):
    el = ET.SubElement(parent, qn(prefix, local), attrib or {})
    if text is not None:
        el.text = text
    return el


def to_ncname(value: str, used: set[str] | None = None) -> str:
    """Turn an arbitrary string into a valid, optionally-deduplicated xs:ID/NCName."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "ITEM")
    if not name or not (name[0].isalpha() or name[0] == "_"):
        name = f"_{name}"
    if used is None:
        return name
    candidate, i = name, 2
    while candidate in used:
        candidate = f"{name}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def looks_like_scene_file(value: str) -> bool:
    return bool(SCENE_FILE_RE.search(value or ""))


class LayerTimeline:
    """Sparse frame -> cel-or-None state, queried with hold-until-changed semantics."""

    def __init__(self):
        self._frames: list[int] = []
        self._states: list[str | None] = []

    def add(self, frame_no: int, state: str | None) -> None:
        self._frames.append(frame_no)
        self._states.append(state)

    def finalize(self) -> None:
        order = sorted(range(len(self._frames)), key=lambda i: self._frames[i])
        self._frames = [self._frames[i] for i in order]
        self._states = [self._states[i] for i in order]

    def at(self, frame_no: int) -> str | None:
        i = bisect.bisect_right(self._frames, frame_no) - 1
        return self._states[i] if i >= 0 else None

    @property
    def frames(self) -> list[int]:
        return self._frames


def build_layer_timelines(layers: list[dict]) -> tuple[dict[str, LayerTimeline], dict[str, int], dict[str, str]]:
    timelines: dict[str, LayerTimeline] = {}
    order: dict[str, int] = {}
    categories: dict[str, str] = {}

    for idx, layer in enumerate(layers or []):
        name = layer.get("name") or f"Layer{idx}"
        categories[name] = layer.get("type", "character")
        timeline = LayerTimeline()
        frames = sorted(layer.get("frames", []), key=lambda e: e["frame"])
        for i, entry in enumerate(frames):
            start = entry["frame"]
            exposure = max(entry.get("exposure", 1), 1)
            timeline.add(start, entry.get("cell"))
            end_exclusive = start + exposure
            next_start = frames[i + 1]["frame"] if i + 1 < len(frames) else None
            if next_start is None or next_start > end_exclusive:
                timeline.add(end_exclusive, None)
        timeline.finalize()
        timelines[name] = timeline
        order[name] = idx

    return timelines, order, categories


def build_notes_by_frame(marks: list[dict]) -> dict[int, str]:
    notes: dict[int, list[str]] = {}
    for mark in marks or []:
        text = f"{mark['type']}: {mark['note']}" if mark.get("note") else mark["type"]
        notes.setdefault(mark["frame"], []).append(text)
    return {frame: "; ".join(parts) for frame, parts in notes.items()}


def infer_track_type(file_name: str, description: str | None) -> str:
    text = f"{file_name} {description or ''}".lower()
    if any(k in text for k in ("dialog", "voice", "vo_", "speech", "vox")):
        return "Dialogue"
    if any(k in text for k in ("music", "bgm", "score", "theme")):
        return "Music"
    return "Effects"


def build_audio(root, audio_entries: list[dict], used_ids: set[str]) -> dict[int, list[dict]]:
    audio_el = sub(root, "audio", "AudioTracks")
    refs: dict[int, list[dict]] = {}

    if not audio_entries:
        audio_el.append(ET.Comment(
            " Placeholder: source XDTS-Extended file has no audio entries; synthesized to satisfy the schema. "
        ))
        sub(audio_el, "audio", "Track", {"id": "AUDIO_PLACEHOLDER", "type": "Effects", "file": "unknown.wav"})
        return refs

    for i, audio in enumerate(audio_entries):
        file_name = audio["file"]
        stem = Path(file_name).stem or f"AUDIO{i}"
        track_id = to_ncname(stem.upper(), used_ids)
        track_type = infer_track_type(file_name, audio.get("description"))
        sub(audio_el, "audio", "Track", {"id": track_id, "type": track_type, "file": file_name})

        start = audio["startFrame"]
        end = start + max(audio["durationFrames"], 1) - 1
        refs.setdefault(start, []).append({"track": track_id, "startFrame": start, "endFrame": end})

    return refs


def build_camera(root, camera_entries: list[dict], camera_name: str, start_frame: int, used_ids: set[str]) -> None:
    camera_id = to_ncname((camera_name or "CAM1").upper(), used_ids)
    camera_el = sub(root, "cam", "Camera", {"cameraId": camera_id, "name": camera_name or "Camera"})

    if not camera_entries:
        sub(camera_el, "cam", "Keyframe", {
            "frame": str(start_frame),
            "note": "Synthesized placeholder - source XDTS-Extended file has no camera entries.",
        })
        return

    for entry in sorted(camera_entries, key=lambda c: c["frame"]):
        attrs = {"frame": str(entry["frame"])}
        position = entry.get("position") or {}
        if "x" in position:
            attrs["x"] = str(position["x"])
        if "y" in position:
            attrs["y"] = str(position["y"])
        if "z" in position:
            attrs["z"] = str(position["z"])
        if "zoom" in entry:
            attrs["zoom"] = str(entry["zoom"])
        if "rotation" in entry:
            attrs["rotationZ"] = str(entry["rotation"])
        if "tilt" in entry:
            attrs["rotationX"] = str(entry["tilt"])
        sub(camera_el, "cam", "Keyframe", attrs)


def build_assets(root, layer_names: list[str], categories: dict[str, str], used_ids: set[str]) -> None:
    assets_el = sub(root, "asset", "Assets")
    names = layer_names or ["Layer0"]
    for name in names:
        category = (categories.get(name) or "character").capitalize()
        sub(assets_el, "asset", "Asset", {
            "id": to_ncname(name, used_ids),
            "name": name,
            "category": category,
            "version": "1",
        })


def build_timeline(root, timelines: dict[str, LayerTimeline], layer_order: dict[str, int],
                    notes_by_frame: dict[int, str], audio_refs: dict[int, list[dict]],
                    start_frame: int, end_frame: int, used_ids: set[str]) -> None:
    trigger_frames = set(notes_by_frame) | set(audio_refs)
    for timeline in timelines.values():
        trigger_frames.update(timeline.frames)
    trigger_frames = {f for f in trigger_frames if start_frame <= f <= end_frame}
    if not trigger_frames:
        trigger_frames = {start_frame}

    timeline_el = sub(root, "core", "Timeline")
    ordered_layers = sorted(timelines, key=lambda n: layer_order.get(n, 0))

    for frame_no in sorted(trigger_frames):
        frame_el = sub(timeline_el, "core", "Frame", {"number": str(frame_no)})
        layers_el = sub(frame_el, "core", "Layers")

        active = []
        for name in ordered_layers:
            value = timelines[name].at(frame_no)
            if value is None:
                continue
            attrs = {
                "id": name,
                "assetRef": to_ncname(name),
                "type": "3D" if looks_like_scene_file(value) else "2D",
                "zOrder": str(layer_order.get(name, 0)),
            }
            attrs["sceneFile" if looks_like_scene_file(value) else "cel"] = value
            sub(layers_el, "core", "Layer", attrs)
            active.append(name)

        if not active:
            placeholder = to_ncname("_placeholder", used_ids)
            sub(layers_el, "core", "Layer", {
                "id": placeholder, "assetRef": placeholder, "type": "2D",
                "cel": "UNKNOWN", "zOrder": "0",
            })

        for ref in audio_refs.get(frame_no, []):
            sub(frame_el, "core", "AudioRef", {
                "track": ref["track"],
                "startFrame": str(ref["startFrame"]),
                "endFrame": str(ref["endFrame"]),
            })

        sub(frame_el, "core", "Notes", text=notes_by_frame.get(frame_no, ""))


def build_reviews_placeholder(root, start_frame: int) -> None:
    reviews_el = sub(root, "review", "Reviews")
    reviews_el.append(ET.Comment(
        " Placeholder: XDTS-Extended carries no review data; synthesized to satisfy the schema. "
    ))
    review_el = sub(reviews_el, "review", "Review", {
        "id": "RV_PLACEHOLDER", "frame": str(start_frame),
        "reviewer": "unknown", "status": "Pending",
    })
    sub(review_el, "review", "Comment", text="No review data available in source XDTS-Extended file.")


def build_version_control_placeholder(root) -> None:
    vc_el = sub(root, "core", "VersionControl")
    vc_el.append(ET.Comment(
        " Placeholder: XDTS-Extended carries no revision history; synthesized to satisfy the schema. "
    ))
    sub(vc_el, "core", "Revision", {
        "number": "1", "author": "xdts_extended_to_xsheet.py",
        "date": datetime.date.today().isoformat(),
    }, text="Auto-generated from XDTS-Extended conversion; original revision history not available.")


def build_otio_placeholder(root, shot_id: str) -> None:
    otio_el = sub(root, "otio", "OTIO")
    sub(otio_el, "otio", "TimelineRef", text=f"{shot_id}.otio")


def convert_document(data: dict, args) -> ET.Element:
    layers = data.get("layers", [])
    marks = data.get("marks", [])
    camera_entries = data.get("camera", [])
    audio_entries = data.get("audio", [])

    timelines, layer_order, categories = build_layer_timelines(layers)
    notes_by_frame = build_notes_by_frame(marks)

    frame_numbers = [1]
    for layer in layers:
        for entry in layer.get("frames", []):
            frame_numbers.append(entry["frame"])
            frame_numbers.append(entry["frame"] + max(entry.get("exposure", 1), 1) - 1)
    for mark in marks:
        frame_numbers.append(mark["frame"])
    for cam in camera_entries:
        frame_numbers.append(cam["frame"])
    for audio in audio_entries:
        frame_numbers.append(audio["startFrame"])
        frame_numbers.append(audio["startFrame"] + max(audio["durationFrames"], 1) - 1)

    start_frame = min(frame_numbers)
    end_frame = max(frame_numbers)

    scene = data.get("scene", "UNKNOWN")
    cut = data.get("cut", "UNKNOWN")
    frame_rate = args.frame_rate if args.frame_rate is not None else data.get("frameRate", 24)

    shot_id = args.shot_id or cut
    scene_id = args.scene_id or scene
    title = args.title or f"{scene} {cut}".strip()

    root = ET.Element(qn("core", "ExposureSheet"), {
        "version": args.xsheet_version,
        qn("xsi", "schemaLocation"): " ".join(
            f"{NS[p]} {SCHEMA_LOCATIONS[p]}" for p in ("core", "asset", "audio", "cam", "review", "otio")
        ),
    })

    prod_el = sub(root, "core", "Production")
    sub(prod_el, "core", "ProjectID", text=args.project_id)
    sub(prod_el, "core", "SequenceID", text=args.sequence_id)
    sub(prod_el, "core", "SceneID", text=scene_id)
    sub(prod_el, "core", "ShotID", text=shot_id)
    sub(prod_el, "core", "Title", text=title)
    sub(prod_el, "core", "FrameRate", text=str(frame_rate))
    sub(prod_el, "core", "StartFrame", text=str(start_frame))
    sub(prod_el, "core", "EndFrame", text=str(end_frame))

    used_ids: set[str] = set()
    layer_names = [layer.get("name") or f"Layer{i}" for i, layer in enumerate(layers)] or ["Layer0"]

    build_assets(root, layer_names, categories, used_ids)
    audio_refs = build_audio(root, audio_entries, used_ids)
    build_camera(root, camera_entries, args.camera_name, start_frame, used_ids)
    build_timeline(root, timelines, layer_order, notes_by_frame, audio_refs, start_frame, end_frame, used_ids)
    build_reviews_placeholder(root, start_frame)
    build_version_control_placeholder(root)
    build_otio_placeholder(root, shot_id)

    return root


def render(root: ET.Element, data: dict, source_path: Path) -> str:
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")

    comments = [
        f"Auto-generated by xdts_extended_to_xsheet.py from {source_path.name}.",
        f"Source scene/cut: {data.get('scene', '?')} / {data.get('cut', '?')}.",
        "The following sections are synthesized placeholders (no XDTS-Extended equivalent exists):",
        "ProjectID / SequenceID (CLI-overridable), Reviews, VersionControl, OTIO/TimelineRef,",
        "plus AudioTracks/Camera contents only when the source file has no audio/camera entries.",
    ]
    header = "\n".join(f"<!-- {line} -->" for line in comments)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n\n{header}\n\n{body}\n'


def validate_input_schema(data: dict, schema_path: Path) -> None:
    """Validate raw XDTS-Extended JSON against its schema, raising on failure
    (jsonschema.ValidationError, or another exception if the schema itself
    can't be loaded)."""
    import jsonschema

    with schema_path.open(encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(instance=data, schema=schema)


def validate_with_xmllint(xml_path: Path, schema_path: Path) -> bool:
    xmllint = shutil.which("xmllint")
    if not xmllint:
        print("xmllint not found; skipping --validate.", file=sys.stderr)
        return True
    result = subprocess.run(
        [xmllint, "--noout", "--schema", str(schema_path), str(xml_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="XDTS-Extended JSON file")
    parser.add_argument("-o", "--output", type=Path, help="Output XML path (default: alongside input)")
    parser.add_argument("--project-id", default="UNKNOWN", help="Production/ProjectID (not present in XDTS-Extended)")
    parser.add_argument("--sequence-id", default="UNKNOWN", help="Production/SequenceID (not present in XDTS-Extended)")
    parser.add_argument("--shot-id", help="Production/ShotID override (default: the source's 'cut' value)")
    parser.add_argument("--scene-id", help="Production/SceneID override (default: the source's 'scene' value)")
    parser.add_argument("--title", help="Production/Title override (default: '<scene> <cut>')")
    parser.add_argument("--camera-name", default="Camera", help="cam:Camera/@name (not present in XDTS-Extended)")
    parser.add_argument("--frame-rate", type=int, help="Production/FrameRate override (default: source frameRate)")
    parser.add_argument("--xsheet-version", default="1.0", help="ExposureSheet/@version to emit")
    parser.add_argument("--validate", action="store_true", help="Validate the output XML against xml/xsheet-core.xsd")
    parser.add_argument("--validate-input", action="store_true",
                         help="Validate the input JSON against tools/xdts-extended.schema.json before converting")
    args = parser.parse_args()

    with args.input.open(encoding="utf-8") as f:
        data = json.load(f)

    if args.validate_input:
        try:
            validate_input_schema(data, DEFAULT_INPUT_SCHEMA_PATH)
        except Exception as exc:  # jsonschema.ValidationError or similar
            print(f"Input validation failed: {exc}", file=sys.stderr)
            return 1

    root = convert_document(data, args)
    xml_text = render(root, data, args.input)

    output_path = args.output or args.input.with_suffix(".xml")
    output_path.write_text(xml_text, encoding="utf-8")
    print(f"Wrote {output_path}")

    ok = True
    if args.validate:
        schema_path = Path(__file__).resolve().parent.parent / "xml" / "xsheet-core.xsd"
        ok = validate_with_xmllint(output_path, schema_path)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
