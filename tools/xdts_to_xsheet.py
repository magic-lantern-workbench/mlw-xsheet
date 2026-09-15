#!/usr/bin/env python3
"""Convert an XDTS JSON timesheet (tools/xdts-20261206.json) back into an
XSheet ExposureSheet XML document (xml/xsheet-core.xsd).

This is the inverse of xsheet_to_xdts.py. The two formats are not
information-equivalent -- XDTS has no room for several things an
ExposureSheet requires (audio tracks, reviews, version history, per-asset
metadata, numeric camera keyframes, project/sequence identifiers, frame
rate) -- so this converter synthesizes clearly-labeled placeholders where
needed to produce schema-valid output, rather than emitting an invalid
document. Every synthesized section is called out in a comment block at
the top of the generated file.

Mapping decisions (mirroring xsheet_to_xdts.py's choices in reverse):

- fieldId=0 ("Cell") tracks become one <Layer> per track, ordered by
  trackNo (0 = bottom, per the XDTS spec => Layer/@zOrder). A track's
  value is held across frames until the next entry, matching XDTS
  semantics; SYMBOL_HYPHEN means "no change" (skipped), SYMBOL_NULL_CELL
  means the layer is absent from that frame onward, and SYMBOL_TICK_1 /
  SYMBOL_TICK_2 (inbetween / reverse-sheet marks) are passed through
  literally as the cel value since XSheet has no dedicated slot for them.
  A cel value that looks like a 3D scene file (.usd/.abc/.fbx/...) is
  reconstructed as a 3D layer using @sceneFile instead of @cel.
- fieldId=3 ("Dialog") entries become <Dialogue phoneme="values[0]">,
  using values[1] (if present) as the text -- the inverse of this
  project's forward [phoneme, text] convention. SYMBOL_HYPHEN entries are
  dropped since XSheet dialogue is a discrete per-frame element, not a
  held range.
- fieldId=5 ("Camerawork") entries become <CameraMove> segments: a
  "TYPE: description" string is split back into type/description when
  TYPE matches the CameraMoveEnum, otherwise the whole string becomes a
  CUSTOM move's description. A segment runs from its own frame to just
  before the next entry (or the end of the sheet). Numeric keyframe data
  has no XDTS source, so exactly one placeholder <Keyframe> is
  synthesized (Camera requires at least one).
- ProjectID, SequenceID, FrameRate, AudioTracks, Reviews, VersionControl
  and OTIO have no XDTS equivalent and are filled with placeholders
  (overridable via CLI flags where it makes sense).

Usage:
    python3 xdts_to_xsheet.py INPUT.xdts.json [-o OUTPUT.xml] [--validate]
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

FIELD_CELL = 0
FIELD_DIALOG = 3
FIELD_CAMERAWORK = 5

SYMBOL_HYPHEN = "SYMBOL_HYPHEN"
SYMBOL_NULL_CELL = "SYMBOL_NULL_CELL"
SYMBOL_TICKS = {"SYMBOL_TICK_1", "SYMBOL_TICK_2"}

CAMERA_MOVE_TYPES = {
    "HOLD", "PAN_LEFT", "PAN_RIGHT", "PAN_UP", "PAN_DOWN",
    "TRUCK_LEFT", "TRUCK_RIGHT", "TRUCK_FORWARD", "TRUCK_BACK",
    "ZOOM_IN", "ZOOM_OUT", "TILT_UP", "TILT_DOWN", "ROLL", "CUSTOM",
}

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


def field_by_id(fields: list[dict], field_id: int) -> dict | None:
    return next((f for f in fields or [] if f.get("fieldId") == field_id), None)


def names_for(headers: list[dict], field_id: int) -> list[str]:
    header = next((h for h in headers or [] if h.get("fieldId") == field_id), None)
    return list(header["names"]) if header else []


def track_entries(track: dict) -> list[tuple[int, list[str]]]:
    frames = sorted(track.get("frames", []), key=lambda e: e["frame"])
    out = []
    for entry in frames:
        data = entry.get("data") or []
        values = data[-1].get("values", []) if data else []
        out.append((entry["frame"], values))
    return out


class LayerTimeline:
    """Sparse frame -> cel-or-None state, queried with hold-until-changed semantics."""

    def __init__(self):
        self._frames: list[int] = []
        self._states: list[str | None] = []

    def add(self, frame_idx: int, state: str | None) -> None:
        self._frames.append(frame_idx)
        self._states.append(state)

    def finalize(self) -> None:
        order = sorted(range(len(self._frames)), key=lambda i: self._frames[i])
        self._frames = [self._frames[i] for i in order]
        self._states = [self._states[i] for i in order]

    def at(self, frame_idx: int) -> str | None:
        i = bisect.bisect_right(self._frames, frame_idx) - 1
        return self._states[i] if i >= 0 else None

    @property
    def frames(self) -> list[int]:
        return self._frames


def build_layer_timelines(cell_field: dict | None, names: list[str]) -> tuple[dict[str, LayerTimeline], dict[str, int], set[int]]:
    timelines: dict[str, LayerTimeline] = {}
    order: dict[str, int] = {}
    trigger_frames: set[int] = set()
    if not cell_field:
        return timelines, order, trigger_frames

    for track in sorted(cell_field.get("tracks", []), key=lambda t: t["trackNo"]):
        track_no = track["trackNo"]
        name = names[track_no] if track_no < len(names) else f"Layer{track_no}"
        timeline = LayerTimeline()
        for frame_idx, values in track_entries(track):
            if not values:
                continue
            value = values[0]
            if value == SYMBOL_HYPHEN:
                continue
            state = None if value == SYMBOL_NULL_CELL else value
            timeline.add(frame_idx, state)
            trigger_frames.add(frame_idx)
        timeline.finalize()
        timelines[name] = timeline
        order[name] = track_no

    return timelines, order, trigger_frames


def build_dialog_by_frame(dialog_field: dict | None) -> dict[int, tuple[str, str]]:
    result: dict[int, tuple[str, str]] = {}
    if not dialog_field:
        return result
    for track in dialog_field.get("tracks", []):
        for frame_idx, values in track_entries(track):
            if not values or values[0] == SYMBOL_HYPHEN:
                continue
            phoneme = values[0]
            text = values[1] if len(values) > 1 else ""
            result[frame_idx] = (phoneme, text)
    return result


def build_camera_moves(camerawork_field: dict | None) -> list[dict]:
    if not camerawork_field:
        return []
    tracks = camerawork_field.get("tracks", [])
    if not tracks:
        return []
    track = min(tracks, key=lambda t: t["trackNo"])
    entries = [(f, v) for f, v in track_entries(track) if v and v[0] != SYMBOL_HYPHEN]
    entries.sort(key=lambda e: e[0])

    moves = []
    for i, (frame_idx, values) in enumerate(entries):
        text = values[0]
        match = re.match(r"^([A-Z_]+): (.*)$", text, re.DOTALL)
        if match and match.group(1) in CAMERA_MOVE_TYPES:
            move_type, description = match.group(1), match.group(2)
        else:
            move_type, description = "CUSTOM", text
        end_idx = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
        moves.append({
            "type": move_type,
            "description": description,
            "startFrameIdx": frame_idx,
            "endFrameIdx": end_idx,
        })
    return moves


def looks_like_scene_file(value: str) -> bool:
    return bool(SCENE_FILE_RE.search(value or ""))


def build_camera(root, camerawork_field: dict | None, camera_names: list[str],
                  start_frame: int, duration: int, used_ids: set[str]) -> None:
    camera_name = camera_names[0] if camera_names else "Camera"
    camera_id = to_ncname(camera_name.upper() if camera_name else "CAM1", used_ids)
    camera_el = sub(root, "cam", "Camera", {"cameraId": camera_id, "name": camera_name})

    for move in build_camera_moves(camerawork_field):
        start = move["startFrameIdx"] + start_frame
        end = (move["endFrameIdx"] + start_frame) if move["endFrameIdx"] is not None else (start_frame + duration - 1)
        move_el = sub(camera_el, "cam", "CameraMove", {
            "type": move["type"],
            "startFrame": str(start),
            "endFrame": str(max(end, start)),
        })
        if move["description"]:
            sub(move_el, "cam", "Description", text=move["description"])

    sub(camera_el, "cam", "Keyframe", {
        "frame": str(start_frame),
        "note": "Synthesized placeholder - XDTS Camerawork stores text instructions only; "
                "no numeric keyframe data is recoverable from the source file.",
    })


def build_timeline(root, timelines: dict[str, LayerTimeline], layer_order: dict[str, int],
                    dialog_by_frame: dict[int, tuple[str, str]], start_frame: int,
                    used_ids: set[str]) -> None:
    trigger_frames = set(dialog_by_frame)
    for timeline in timelines.values():
        trigger_frames.update(timeline.frames)
    if not trigger_frames:
        trigger_frames = {0}

    timeline_el = sub(root, "core", "Timeline")
    ordered_layers = sorted(timelines, key=lambda n: layer_order.get(n, 0))

    for frame_idx in sorted(trigger_frames):
        frame_el = sub(timeline_el, "core", "Frame", {"number": str(frame_idx + start_frame)})
        layers_el = sub(frame_el, "core", "Layers")

        active = []
        for name in ordered_layers:
            value = timelines[name].at(frame_idx)
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

        if frame_idx in dialog_by_frame:
            phoneme, text = dialog_by_frame[frame_idx]
            sub(frame_el, "core", "Dialogue", {"phoneme": phoneme}, text=text)

        sub(frame_el, "core", "Notes", text="")


def build_assets(root, layer_names: list[str]) -> None:
    assets_el = sub(root, "asset", "Assets")
    used: set[str] = set()
    names = layer_names or ["Layer0"]
    for name in names:
        sub(assets_el, "asset", "Asset", {
            "id": to_ncname(name, used),
            "name": name,
            "category": "Unknown",
            "version": "1",
        })


def build_audio_placeholder(root) -> None:
    audio_el = sub(root, "audio", "AudioTracks")
    audio_el.append(ET.Comment(
        " Placeholder: XDTS carries no audio track data; synthesized to satisfy the schema. "
    ))
    sub(audio_el, "audio", "Track", {"id": "AUDIO_PLACEHOLDER", "type": "Effects", "file": "unknown.wav"})


def build_reviews_placeholder(root, start_frame: int) -> None:
    reviews_el = sub(root, "review", "Reviews")
    reviews_el.append(ET.Comment(
        " Placeholder: XDTS carries no review data; synthesized to satisfy the schema. "
    ))
    review_el = sub(reviews_el, "review", "Review", {
        "id": "RV_PLACEHOLDER", "frame": str(start_frame),
        "reviewer": "unknown", "status": "Pending",
    })
    sub(review_el, "review", "Comment", text="No review data available in source XDTS file.")


def build_version_control_placeholder(root) -> None:
    vc_el = sub(root, "core", "VersionControl")
    vc_el.append(ET.Comment(
        " Placeholder: XDTS carries no revision history; synthesized to satisfy the schema. "
    ))
    sub(vc_el, "core", "Revision", {
        "number": "1", "author": "xdts_to_xsheet.py",
        "date": datetime.date.today().isoformat(),
    }, text="Auto-generated from XDTS conversion; original revision history not available.")


def build_otio_placeholder(root, shot_id: str) -> None:
    otio_el = sub(root, "otio", "OTIO")
    sub(otio_el, "otio", "TimelineRef", text=f"{shot_id}.otio")


def convert_timetable(table: dict, header: dict, xdts_version: int, args) -> ET.Element:
    fields = table.get("fields", [])
    headers = table.get("timeTableHeaders", [])
    duration = table.get("duration", 1)

    cell_names = names_for(headers, FIELD_CELL)
    camera_names = names_for(headers, FIELD_CAMERAWORK)

    cell_field = field_by_id(fields, FIELD_CELL)
    dialog_field = field_by_id(fields, FIELD_DIALOG)
    camerawork_field = field_by_id(fields, FIELD_CAMERAWORK)

    timelines, layer_order, _ = build_layer_timelines(cell_field, cell_names)
    dialog_by_frame = build_dialog_by_frame(dialog_field)

    start_frame = 1
    end_frame = start_frame + max(duration, 1) - 1

    cut = args.shot_id or (header.get("cut") if header else None) or "0"
    scene = args.scene_id or (header.get("scene") if header else None) or "0"
    shot_id = args.shot_id or f"SH{cut}"
    scene_id = args.scene_id or f"SC{scene}"

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
    sub(prod_el, "core", "Title", text=table.get("name", "Untitled"))
    sub(prod_el, "core", "FrameRate", text=str(args.frame_rate))
    sub(prod_el, "core", "StartFrame", text=str(start_frame))
    sub(prod_el, "core", "EndFrame", text=str(end_frame))

    build_assets(root, cell_names)
    build_audio_placeholder(root)
    build_camera(root, camerawork_field, camera_names, start_frame, duration, used_ids=set())

    used_ids: set[str] = set()
    build_timeline(root, timelines, layer_order, dialog_by_frame, start_frame, used_ids)

    build_reviews_placeholder(root, start_frame)
    build_version_control_placeholder(root)
    build_otio_placeholder(root, shot_id)

    return root


def render(root: ET.Element, table: dict, source_path: Path, xdts_version: int) -> str:
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")

    comments = [
        f"Auto-generated by xdts_to_xsheet.py from {source_path.name} (XDTS version {xdts_version}).",
        f"Source timesheet: {table.get('name', 'Untitled')!r}.",
        "The following sections are synthesized placeholders (no XDTS equivalent exists):",
        "ProjectID / SequenceID / FrameRate (CLI-overridable), AudioTracks, Reviews,",
        "VersionControl, OTIO/TimelineRef, and the Camera's single Keyframe.",
    ]
    header = "\n".join(f"<!-- {line} -->" for line in comments)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n\n{header}\n\n{body}\n'


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
    parser.add_argument("input", type=Path, help="XDTS JSON file")
    parser.add_argument("-o", "--output", type=Path, help="Output XML path (single table) or directory (multiple tables)")
    parser.add_argument("--index", type=int, help="Convert only timeTables[INDEX] (0-based); default: convert all")
    parser.add_argument("--project-id", default="UNKNOWN", help="Production/ProjectID (not present in XDTS)")
    parser.add_argument("--sequence-id", default="UNKNOWN", help="Production/SequenceID (not present in XDTS)")
    parser.add_argument("--shot-id", help="Production/ShotID override (default: derived from header.cut)")
    parser.add_argument("--scene-id", help="Production/SceneID override (default: derived from header.scene)")
    parser.add_argument("--frame-rate", type=int, default=24, help="Production/FrameRate (not present in XDTS)")
    parser.add_argument("--xsheet-version", default="1.0", help="ExposureSheet/@version to emit")
    parser.add_argument("--validate", action="store_true", help="Validate each output file against xml/xsheet-core.xsd")
    args = parser.parse_args()

    with args.input.open(encoding="utf-8") as f:
        data = json.load(f)

    header = data.get("header", {})
    xdts_version = data.get("version")
    tables = data.get("timeTables", [])
    if not tables:
        print("No timeTables found in input.", file=sys.stderr)
        return 1

    indices = [args.index] if args.index is not None else list(range(len(tables)))
    schema_path = Path(__file__).resolve().parent.parent / "xml" / "xsheet-core.xsd"

    ok = True
    for i in indices:
        table = tables[i]
        root = convert_timetable(table, header, xdts_version, args)
        xml_text = render(root, table, args.input, xdts_version)

        if len(indices) > 1:
            out_dir = args.output if args.output else args.input.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / f"{args.input.stem}-{i}.xml"
        else:
            output_path = args.output or args.input.with_suffix(".xml")

        output_path.write_text(xml_text, encoding="utf-8")
        print(f"Wrote {output_path}")

        if args.validate:
            ok = validate_with_xmllint(output_path, schema_path) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
