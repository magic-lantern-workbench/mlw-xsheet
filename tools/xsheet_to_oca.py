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
Open Cel Animation (OCA) document, version 1.3.0 (https://oca.rxlab.guide).

An OCA document is a folder holding a JSON data file of the same name, an
optional ``_meta.json`` sidecar, and one sub-folder of images per layer:

    NAME.oca/
        NAME.oca             the Root (Document) Object, 4-space indented UTF-8 JSON
        NAME_meta.json       the Metadata Object
        BG/BG001.png         one image per distinct cel of each layer
        CHAR/A001.png
        ...

This module is used two ways, like the XDTS converters:

- As a standalone CLI tool (see ``main()`` / ``if __name__ == "__main__"``
  below).
- As a library imported by the Editor (main.py) to power its File > Export
  OCA feature, via ``export_oca()``, which converts in-editor text (no file
  on disk required) and writes, and checks, the whole OCA folder. The
  building blocks are also available: ``convert_string()`` /
  ``convert_element()`` give the OCA data and metadata as dicts,
  ``write_oca()`` writes a folder and ``check_oca()`` checks one.

The two formats model an exposure sheet differently, so the converter makes
these mapping decisions:

- Document: ``name`` is the Production Title (else the IDs, else the file
  name), ``frameRate`` is FrameRate, and the timeline keeps the XSheet's own
  frame numbers: ``startTime`` is the first frame (StartFrame, or an earlier
  <Frame>) and ``endTime`` -- exclusive in OCA -- is one past the last
  (EndFrame, or a later <Frame>). XSheet has no picture size, so ``width``
  and ``height`` come from the command line (default 1920x1080).
- Layers: one ``paintlayer`` per XSheet layer id, bottom to top by ascending
  zOrder (OCA lists layers from the bottom). A 3D layer's scene file is
  exposed as a rendered image of it, and its layer is marked 3D in ``meta``.
- Frames: XSheet only records a layer's cel where it changes, and holds it
  until the next <Frame> that lists the layer. Each held run becomes one OCA
  frame with a ``duration``; a <Frame> that no longer lists the layer ends
  the run (as the XDTS converters treat it), and the frames before a layer
  first appears or after it drops out are ``_blank`` frames. A cel reused
  later points at the same image file.
- Images: the ExposureSheet names cels but holds no pictures, and OCA needs
  an image for every frame. By default the converter writes a labelled,
  transparent placeholder PNG (layer and cel name) of the document size for
  each distinct cel; ``--cels-dir`` copies real images instead where it
  finds ``DIR/LAYER/CEL.png`` or ``DIR/CEL.png``, and ``--images none``
  writes only the JSON.
- Everything OCA has no attribute for -- production IDs, assets, dialogue,
  notes, camera moves and keyframes, audio cues, reviews and the revision
  history -- goes into the ``meta`` objects the OCA spec provides for custom
  data (the sidecar's ``meta.xsheet``, and each layer's and frame's
  ``meta.xsheet``), so nothing is lost. The OCA data file itself only holds
  attributes the spec lists.

Usage:
    python3 xsheet_to_oca.py INPUT.xml [-o OUTPUT.oca] [--width W] [--height H]
                             [--images {placeholder,none}] [--cels-dir DIR] [--validate]
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import struct
import sys
import uuid
import zlib
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

OCA_VERSION = "1.3.0"
BLANK = "_blank"
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1920, 1080
EXPORTER = {
    "exportedBy": "mlw-xsheet xsheet_to_oca.py",
    "exportedByID": "https://github.com/magic-lantern-workbench/mlw-xsheet",
    "exportedByOrg": "Wizzer Works",
    "exportedByURL": "https://github.com/magic-lantern-workbench/mlw-xsheet",
}
# Stable ids: the same document always converts to the same layer and frame ids.
ID_NAMESPACE = uuid.UUID("7d1c4a52-1f7e-4c35-9b8e-6f0a2d5c3e91")


class XSheetConversionError(Exception):
    """Raised when an XSheet document cannot be parsed or converted to OCA."""


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def text_of(el) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def int_or(value, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_name(value: str) -> str:
    """A file or folder name for a layer or cel: characters that aren't
    allowed (or mean something) in paths are replaced with '_'."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip().strip(".")
    return name or "_"


# ---------------------------------------------------------------- reading XSheet

def parse_production(root) -> dict:
    prod = root.find(q("core", "Production"))
    fields = ("ProjectID", "SequenceID", "SceneID", "ShotID", "Title", "FrameRate", "StartFrame", "EndFrame")
    return {name: text_of(prod.find(q("core", name))) if prod is not None else "" for name in fields}


def parse_frames(root) -> list[dict]:
    timeline = root.find(q("core", "Timeline"))
    frames = []
    for frame_el in (timeline.findall(q("core", "Frame")) if timeline is not None else []):
        number = int_or(frame_el.get("number"))
        if number is None:
            continue
        layers = []
        layers_el = frame_el.find(q("core", "Layers"))
        for layer_el in (layers_el.findall(q("core", "Layer")) if layers_el is not None else []):
            layers.append(dict(layer_el.attrib))
        dialogue_el = frame_el.find(q("core", "Dialogue"))
        frames.append({
            "number": number,
            "layers": layers,
            "dialogue": {"phoneme": dialogue_el.get("phoneme", ""), "text": text_of(dialogue_el)}
            if dialogue_el is not None else None,
            "audio": [dict(ref.attrib) for ref in frame_el.findall(q("core", "AudioRef"))],
            "notes": text_of(frame_el.find(q("core", "Notes"))),
        })
    frames.sort(key=lambda f: f["number"])
    return frames


def parse_extras(root) -> dict:
    """The parts of the document OCA has no attributes for, as plain data for ``meta``."""
    extras: dict = {}
    assets = root.find(q("asset", "Assets"))
    if assets is not None:
        extras["assets"] = [dict(a.attrib) for a in assets.findall(q("asset", "Asset"))]
    tracks = root.find(q("audio", "AudioTracks"))
    if tracks is not None:
        extras["audioTracks"] = [dict(t.attrib) for t in tracks.findall(q("audio", "Track"))]
    camera = root.find(q("cam", "Camera"))
    if camera is not None:
        extras["camera"] = {
            **camera.attrib,
            "moves": [{**m.attrib, "description": text_of(m.find(q("cam", "Description")))}
                      for m in camera.findall(q("cam", "CameraMove"))],
            "keyframes": [dict(k.attrib) for k in camera.findall(q("cam", "Keyframe"))],
        }
    reviews = root.find(q("review", "Reviews"))
    if reviews is not None:
        extras["reviews"] = [{**r.attrib, "comment": text_of(r.find(q("review", "Comment")))}
                             for r in reviews.findall(q("review", "Review"))]
    history = root.find(q("core", "VersionControl"))
    if history is not None:
        extras["revisions"] = [{**r.attrib, "text": text_of(r)} for r in history.findall(q("core", "Revision"))]
    otio = root.find(q("otio", "OTIO"))
    if otio is not None:
        extras["otio"] = {
            "timelineRef": text_of(otio.find(q("otio", "TimelineRef"))),
            "trackMaps": [dict(t.attrib) for t in otio.findall(q("otio", "TrackMap"))],
        }
    return extras


# ---------------------------------------------------------------- building OCA

def exposure_runs(frames: list[dict], layer_id: str, start: int, end: int) -> list[dict]:
    """The layer's exposure over [start, end): runs of (first frame, length,
    the <Layer> attributes shown, or None for blank), from the XSheet's
    change-only keyframes (see the module docstring)."""
    changes = []  # (frame number, attrs or None)
    for frame in frames:
        attrs = next((a for a in frame["layers"] if a.get("id") == layer_id), None)
        value = (attrs.get("cel") or attrs.get("sceneFile")) if attrs else None
        changes.append((frame["number"], attrs if value else None))
    runs: list[dict] = []
    current, run_start = None, start
    for number, attrs in changes:
        number = max(number, start)
        same = (attrs is None and current is None) or (
            attrs is not None and current is not None
            and (attrs.get("cel") or attrs.get("sceneFile")) == (current.get("cel") or current.get("sceneFile")))
        if same:
            continue
        if number > run_start:
            runs.append({"start": run_start, "length": number - run_start, "attrs": current})
        current, run_start = attrs, number
    if end > run_start:
        runs.append({"start": run_start, "length": end - run_start, "attrs": current})
    return runs


def build_document(root: ET.Element, *, source_name: str = "untitled",
                   width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> tuple[dict, dict, dict]:
    """Convert a parsed <ExposureSheet> into (OCA root object, OCA metadata
    object, images) where images maps each image's relative path to
    (layer id, cel or scene file, the layer's position from the bottom) --
    the pictures the document refers to."""
    if root.tag != q("core", "ExposureSheet"):
        raise XSheetConversionError("The document is not an XSheet ExposureSheet "
                                    f"(its root element is {root.tag.split('}')[-1]}).")
    production = parse_production(root)
    frames = parse_frames(root)
    numbers = [f["number"] for f in frames]
    start = min([n for n in (int_or(production["StartFrame"]),) if n is not None] + numbers, default=1)
    last = max([n for n in (int_or(production["EndFrame"]),) if n is not None] + numbers, default=start)
    end = last + 1  # OCA's endTime is exclusive
    name = production["Title"] or " ".join(
        v for v in (production["SequenceID"], production["SceneID"], production["ShotID"]) if v) or source_name
    try:
        frame_rate = float(production["FrameRate"]) if production["FrameRate"] else 24.0
    except ValueError:
        frame_rate = 24.0

    # every layer id, with the attributes it first appears with; bottom to top by zOrder
    first_seen: dict[str, dict] = {}
    for frame in frames:
        for attrs in frame["layers"]:
            if attrs.get("id"):
                first_seen.setdefault(attrs["id"], attrs)
    layer_ids = sorted(first_seen, key=lambda lid: (int_or(first_seen[lid].get("zOrder"), 0), lid))

    center = [width // 2, height // 2]
    images: dict[str, tuple[str, str, int]] = {}
    oca_layers = []
    for layer_index, layer_id in enumerate(layer_ids):
        info = first_seen[layer_id]
        folder = safe_name(layer_id)
        oca_frames = []
        for run in exposure_runs(frames, layer_id, start, end):
            attrs = run["attrs"]
            frame_id = str(uuid.uuid5(ID_NAMESPACE, f"{layer_id}/{run['start']}"))
            if attrs is None:
                oca_frames.append({
                    "name": BLANK, "id": frame_id, "fileName": "", "frameNumber": run["start"],
                    "opacity": 1.0, "position": [0, 0], "width": 0, "height": 0, "duration": run["length"],
                    "meta": {},
                })
                continue
            value = attrs.get("cel") or attrs.get("sceneFile")
            file_name = f"{folder}/{safe_name(value)}.png"
            images.setdefault(file_name, (layer_id, value, layer_index))
            oca_frames.append({
                "name": value, "id": frame_id, "fileName": file_name, "frameNumber": run["start"],
                "opacity": 1.0, "position": list(center), "width": width, "height": height,
                "duration": run["length"],
                "meta": {"xsheet": {k: attrs[k] for k in ("cel", "sceneFile", "assetRef", "type") if attrs.get(k)}},
            })
        drawn = [f for f in oca_frames if f["name"] != BLANK]
        oca_layers.append({
            "name": layer_id,
            "id": str(uuid.uuid5(ID_NAMESPACE, layer_id)),
            "frames": oca_frames,
            "childLayers": [],
            "type": "paintlayer",
            "fileType": "png",
            "blendingMode": "normal",
            "animated": len(drawn) > 1,
            "position": list(center),
            "width": width,
            "height": height,
            "label": -1,
            "opacity": 1.0,
            "visible": True,
            "passThrough": False,
            "reference": False,
            "inheritAlpha": False,
            "source": {},
            "meta": {"xsheet": {k: info[k] for k in ("assetRef", "type", "zOrder") if info.get(k)}},
        })

    document = {
        "name": name,
        "frameRate": frame_rate,
        "width": width,
        "height": height,
        "startTime": start,
        "endTime": end,
        "colorDepth": "U8",
        "backgroundColor": [0.0, 0.0, 0.0, 0.0],
        "layers": oca_layers,
        "ocaVersion": OCA_VERSION,
    }

    extras = parse_extras(root)
    revisions = extras.get("revisions") or []
    timeline_extras = {
        "dialogue": [{"frame": f["number"], **f["dialogue"]} for f in frames if f["dialogue"]],
        "notes": [{"frame": f["number"], "text": f["notes"]} for f in frames if f["notes"]],
        "audioCues": [{"frame": f["number"], **ref} for f in frames for ref in f["audio"]],
    }
    metadata = {
        "author": ", ".join(dict.fromkeys(r.get("author", "") for r in revisions if r.get("author"))),
        "copyright": "",
        "created": min((r["date"] + " 00:00:00" for r in revisions if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("date", ""))),
                       default=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "description": " / ".join(v for v in (production["Title"], production["ProjectID"], production["SequenceID"],
                                              production["SceneID"], production["ShotID"]) if v),
        **EXPORTER,
        "license": "",
        "licenseLong": "",
        "licenseURL": "",
        "originApp": "XSheet ExposureSheet XML",
        "originAppVersion": root.get("version") or "0.0.0",
        "meta": {"xsheet": {
            "production": production,
            **{k: v for k, v in timeline_extras.items() if v},
            **extras,
        }},
    }
    return document, metadata, images


def oca_folder(path: Path) -> Path:
    """The OCA folder for an output path: it always ends in ``.oca``."""
    path = Path(path)
    return path if path.suffix == ".oca" else path.with_name(path.name + ".oca")


def is_oca_folder(path: Path) -> bool:
    """Whether ``path`` is an OCA folder: a folder holding its own
    ``NAME.oca`` data file. Only such a folder is ever replaced."""
    path = Path(path)
    stem = path.name[:-4] if path.name.endswith(".oca") else path.name
    return path.is_dir() and (path / f"{stem}.oca").is_file()


def export_oca(xml_text: str, out_dir: Path, *, source_name: str = "untitled",
               width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, image_mode: str = "placeholder",
               cels_dir: Path | None = None, replace: bool = False, validate: bool = True) -> dict:
    """High-level entry point for the Editor's Export OCA feature (and the
    CLI): convert XSheet XML text and write the OCA folder ``out_dir``
    (``.oca`` is added to its name if missing).

    An existing ``out_dir`` is only replaced when ``replace`` is set, and
    only if it is an OCA folder (see ``is_oca_folder()``), so a mistyped
    name can't delete some other folder or file.

    Returns a summary: ``path``, ``layers``, ``exposures`` (non-blank OCA
    frames), ``start`` / ``end`` (the first and last frame), ``copied`` /
    ``placeholders`` (images), ``missing`` (the ``LAYER/CEL.png`` images
    looked for in ``cels_dir`` but not found) and ``problems`` (from ``check_oca()``, when
    ``validate`` is set; empty if the folder complies with OCA 1.3.0).

    Raises XSheetConversionError if the XML can't be parsed or isn't an
    ExposureSheet, the picture size isn't positive, or ``out_dir`` exists
    and can't be replaced; FileExistsError if it exists and ``replace``
    isn't set; OSError if writing fails. Callers (e.g. main.py) are
    expected to catch these and report them to the user.
    """
    if width < 1 or height < 1:
        raise XSheetConversionError("The picture width and height must be 1 or more.")
    if image_mode not in ("placeholder", "none"):
        raise XSheetConversionError(f"Unknown image mode {image_mode!r}.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    document, metadata, images = build_document(root, source_name=source_name, width=width, height=height)

    out_dir = oca_folder(out_dir)
    if out_dir.exists() or out_dir.is_symlink():
        if not replace:
            raise FileExistsError(f"{out_dir} already exists")
        if out_dir.is_symlink() or not is_oca_folder(out_dir):
            raise XSheetConversionError(f"{out_dir.name} already exists and is not an OCA folder, "
                                        "so it was not replaced. Choose another name.")
        shutil.rmtree(out_dir)
    counts = write_oca(document, metadata, images, out_dir, image_mode=image_mode, cels_dir=cels_dir)
    layers = document["layers"]
    return {
        "path": out_dir,
        "layers": len(layers),
        "exposures": sum(1 for layer in layers for f in layer["frames"] if f["name"] != BLANK),
        "start": document["startTime"],
        "end": document["endTime"] - 1,
        **counts,
        "problems": check_oca(document, metadata, out_dir if image_mode != "none" else None) if validate else [],
    }


def convert_element(root: ET.Element, *, source_name: str = "untitled",
                    width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> tuple[dict, dict]:
    """Convert a parsed <ExposureSheet> into (OCA root object, OCA metadata object)."""
    document, metadata, _images = build_document(root, source_name=source_name, width=width, height=height)
    return document, metadata


def convert_string(xml_text: str, **kwargs) -> tuple[dict, dict]:
    """Convert XSheet XML held as a string into (OCA root object, OCA metadata object)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XSheetConversionError(f"XSheet XML is not well-formed: {exc}") from exc
    return convert_element(root, **kwargs)


# ---------------------------------------------------------------- checking OCA

# (attribute, type(s), required) for each OCA object, from the 1.3.0 spec.
_NUM = (int, float)
_SPEC = {
    "root": [("name", str, False), ("frameRate", _NUM, False), ("width", int, False), ("height", int, False),
             ("startTime", int, False), ("endTime", int, False), ("colorDepth", str, False),
             ("backgroundColor", list, False), ("layers", list, False), ("ocaVersion", str, False)],
    "layer": [("name", str, True), ("id", str, False), ("type", str, False), ("animated", bool, False),
              ("visible", bool, False), ("opacity", _NUM, False), ("blendingMode", str, False),
              ("inheritAlpha", bool, False), ("position", list, False), ("width", int, False),
              ("height", int, False), ("frames", list, False), ("childLayers", list, False),
              ("fileType", str, False), ("label", int, False), ("passThrough", bool, False),
              ("reference", bool, False), ("source", dict, False), ("meta", dict, False)],
    "frame": [("name", str, True), ("id", str, False), ("fileName", str, True), ("frameNumber", int, True),
              ("opacity", _NUM, False), ("position", list, False), ("width", int, False),
              ("height", int, False), ("duration", int, False), ("meta", dict, False)],
    "meta": [(k, str, False) for k in ("author", "copyright", "created", "description", "exportedBy",
                                       "exportedByID", "exportedByOrg", "exportedByURL", "license",
                                       "licenseLong", "licenseURL", "originApp", "originAppVersion")]
            + [("meta", dict, False)],
}
# Deprecated since OCA 1.2.0 (they belong in the metadata file) but still accepted.
DEPRECATED = {"root": {"originApp", "originAppVersion"}}
LAYER_TYPES = {"paintlayer", "grouplayer", "vectorlayer", "clonelayer", "ocalayer"}
COLOR_DEPTHS = {"U8", "U16", "F16", "F32"}


def check_oca(document: dict, metadata: dict | None = None, folder: Path | None = None) -> list[str]:
    """Problems with an OCA root object (and metadata object, and the files
    in its folder, when given) against the OCA 1.3.0 spec; empty if none."""
    problems: list[str] = []

    def check_object(obj, kind, where):
        if not isinstance(obj, dict):
            problems.append(f"{where}: must be an object")
            return
        allowed = {name for name, _t, _r in _SPEC[kind]} | DEPRECATED.get(kind, set())
        for name, types, required in _SPEC[kind]:
            if name not in obj:
                if required:
                    problems.append(f"{where}: missing required '{name}'")
                continue
            value = obj[name]
            if isinstance(value, bool) and types is not bool:
                problems.append(f"{where}.{name}: must be {types}, not a boolean")
            elif not isinstance(value, types):
                problems.append(f"{where}.{name}: wrong type {type(value).__name__}")
        extra = set(obj) - allowed
        if extra and kind != "meta":  # the metadata object may have custom fields
            problems.append(f"{where}: attributes not in the spec: {sorted(extra)}")

    check_object(document, "root", "document")
    if document.get("colorDepth", "U8") not in COLOR_DEPTHS:
        problems.append(f"document.colorDepth: {document.get('colorDepth')!r} is not a color depth")
    if document.get("endTime", 240) <= document.get("startTime", 0):
        problems.append("document: endTime must be after startTime")
    names = []

    def walk(layers, where):
        for i, layer in enumerate(layers):
            lw = f"{where}[{i}]"
            check_object(layer, "layer", lw)
            names.append(layer.get("name"))
            if layer.get("type", "paintlayer") not in LAYER_TYPES:
                problems.append(f"{lw}.type: {layer.get('type')!r} is not a layer type")
            for j, frame in enumerate(layer.get("frames", [])):
                fw = f"{lw}.frames[{j}]"
                check_object(frame, "frame", fw)
                if frame.get("name") == BLANK:
                    if frame.get("fileName"):
                        problems.append(f"{fw}: a blank frame must have an empty fileName")
                elif not frame.get("fileName"):
                    problems.append(f"{fw}: only a blank frame may have an empty fileName")
                elif folder is not None and not (folder / frame["fileName"].replace("\\", "/")).is_file():
                    problems.append(f"{fw}: image {frame['fileName']} is missing")
                if frame.get("duration", 1) < 1:
                    problems.append(f"{fw}: duration must be 1 or more")
            walk(layer.get("childLayers", []), f"{lw}.childLayers")

    walk(document.get("layers", []), "document.layers")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append(f"layer names must be unique in the document: {duplicates}")
    if metadata is not None:
        check_object(metadata, "meta", "metadata")
    return problems


# ---------------------------------------------------------------- writing files

def _png_bytes(width: int, height: int) -> bytes:
    """A fully transparent RGBA PNG, written with the standard library only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00" * (width * 4) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_placeholder(path: Path, width: int, height: int, label: str, row: int = 0) -> None:
    """A transparent placeholder of the document size, labelled with the
    layer and cel when Pillow is available (plain transparent otherwise).
    Each layer's label sits on its own row (``row``: the layer's position
    from the bottom), so the stacked layers' labels don't cover each other."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        path.write_bytes(_png_bytes(width, height))
        return
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=max(12, height // 30))
    except (ImportError, TypeError, OSError):
        font = None
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    pad = max(4, height // 100)
    y = pad + row * (bottom - top + pad * 3)
    box = [pad, y, pad * 3 + right - left, y + pad * 2 + bottom - top]
    draw.rectangle(box, fill=(255, 255, 255, 200), outline=(88, 152, 212, 255), width=max(1, pad // 3))
    draw.text((pad * 2 - left, y + pad - top), label, fill=(43, 93, 138, 255), font=font)
    image.save(path, "PNG")


def write_oca(document: dict, metadata: dict, images: dict, out_dir: Path, *,
              image_mode: str = "placeholder", cels_dir: Path | None = None) -> dict:
    """Write the OCA folder ``out_dir`` (``NAME.oca``): the data file
    ``NAME.oca``, the ``NAME_meta.json`` sidecar and the layer images.
    Returns counts of the images copied from ``cels_dir`` and of
    placeholders, and ``missing``: for each placeholder written while a
    ``cels_dir`` was given, the ``LAYER/CEL.png`` it looked for there."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir.name[:-4] if out_dir.name.endswith(".oca") else out_dir.name
    counts = {"copied": 0, "placeholders": 0, "missing": []}
    if image_mode != "none":
        for rel, (layer_id, value, row) in images.items():
            path = out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            source = None
            if cels_dir is not None:
                for candidate in (cels_dir / safe_name(layer_id) / f"{safe_name(value)}.png",
                                  cels_dir / f"{safe_name(value)}.png",
                                  cels_dir / safe_name(layer_id) / safe_name(value),
                                  cels_dir / safe_name(value)):
                    if candidate.is_file() and candidate.suffix.lower() == ".png":
                        source = candidate
                        break
            if source is not None:
                shutil.copyfile(source, path)
                counts["copied"] += 1
            else:
                write_placeholder(path, document["width"], document["height"], f"{layer_id}: {value}", row)
                counts["placeholders"] += 1
                if cels_dir is not None:
                    counts["missing"].append(f"{safe_name(layer_id)}/{safe_name(value)}.png")
    # OCA: UTF-8, pretty printed with a 4-space indent
    (out_dir / f"{stem}.oca").write_text(json.dumps(document, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / f"{stem}_meta.json").write_text(json.dumps(metadata, indent=4, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="XSheet ExposureSheet XML file")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output OCA folder (default: INPUT's name with .oca, alongside it)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"Picture width in pixels (default {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"Picture height in pixels (default {DEFAULT_HEIGHT})")
    parser.add_argument("--images", choices=("placeholder", "none"), default="placeholder",
                        help="Write a labelled placeholder PNG per cel (default), or no images")
    parser.add_argument("--cels-dir", type=Path,
                        help="Folder of real cel images to copy in place of placeholders "
                             "(DIR/LAYER/CEL.png or DIR/CEL.png)")
    parser.add_argument("--validate", action="store_true", help="Check the result against the OCA 1.3.0 spec")
    parser.add_argument("--force", action="store_true",
                        help="Replace the output folder if it already exists (only an OCA folder is replaced)")
    args = parser.parse_args()

    try:
        xml_text = args.input.read_text(encoding="utf-8")
        summary = export_oca(xml_text, args.output or args.input.with_suffix(".oca"), source_name=args.input.stem,
                             width=args.width, height=args.height, image_mode=args.images,
                             cels_dir=args.cels_dir, replace=args.force, validate=args.validate)
    except FileExistsError as exc:
        print(f"{exc}; use --force to replace it", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError, XSheetConversionError) as exc:
        print(f"Could not convert {args.input}: {exc}", file=sys.stderr)
        return 1

    if summary["problems"]:
        print("OCA check failed:\n  " + "\n  ".join(summary["problems"]), file=sys.stderr)
        return 1
    print(f"Wrote {summary['path']}: {summary['layers']} layer(s), {summary['exposures']} exposure(s), "
          f"frames {summary['start']}-{summary['end']}; "
          f"{summary['copied']} image(s) copied, {summary['placeholders']} placeholder(s)"
          + ("; OCA check passed" if args.validate else ""))
    if summary["missing"]:
        print(f"Not found in {args.cels_dir} (as LAYER/CEL.png or CEL.png): " + ", ".join(summary["missing"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
