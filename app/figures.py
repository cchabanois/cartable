"""Figures on cards: geometry, labelled and measured figures, drawn as SVG by the AI
that writes the cards (image models draw text and exact measures badly).

The cards say what to draw (Card.figure, a description); each figure is then drawn
by a short call of its own with the drawing rules below, cleaned (only shapes and
text: nothing that runs or loads anything) and saved next to the card pictures.
"""

import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from .errors import AppError
from .models import Card

SVG_NS = "http://www.w3.org/2000/svg"
ElementTree.register_namespace("", SVG_NS)
MAX_SIZE = 200_000  # characters: a figure is a few kilobytes
VIEWBOX = "0 0 400 300"

RULES = """\
You draw figures for a pupil's flashcards, as SVG: geometry, labelled or measured \
figures, simple diagrams. Rules:
- one <svg> element with viewBox="0 0 400 300", no width or height attributes;
- a white background first: <rect width="400" height="300" fill="white"/>;
- draw exactly what the description asks for, nothing more: no title, no caption, no \
label or measure it doesn't mention (the figure goes on a flashcard: one word too many \
may give the answer away);
- black strokes, font-family="sans-serif", font-size 14 to 16;
- labels never overlap lines or each other; leave margins inside the viewBox;
- exact geometry: right angles really right (marked with a small square), \
proportions and measures as described;
- plain text and Unicode only (√, ², π, α, °), no LaTeX; the labels in the \
description's language, written exactly as given;
- only shapes, text and arrow markers: no script, no image, no link, no external \
reference, no animation.
Answer in "svg" with the SVG document only."""


class Drawing(BaseModel):
    svg: str = Field(description="The SVG document, starting with <svg.")


class FigureError(AppError):
    status = 502


# --- Cleaning: an allow-list of shapes, text and their presentation ----------------

ELEMENTS = {
    "svg", "g", "defs", "marker", "title", "desc",
    "path", "line", "polyline", "polygon", "rect", "circle", "ellipse", "text", "tspan",
}  # fmt: skip
GEOMETRY = {
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points",
    "dx", "dy", "rotate", "transform", "viewBox", "preserveAspectRatio", "id",
    "refX", "refY", "markerWidth", "markerHeight", "orient", "markerUnits", "textLength", "lengthAdjust",
}  # fmt: skip
PRESENTATION = {
    "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "fill-opacity", "stroke-opacity", "opacity", "fill-rule",
    "font-family", "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline",
    "alignment-baseline", "baseline-shift", "letter-spacing", "word-spacing", "text-decoration",
    "marker-start", "marker-mid", "marker-end", "visibility", "display",
}  # fmt: skip
MARKER_REF = re.compile(r"^url\(#[\w.-]+\)$")
SAFE_VALUE = re.compile(r"^[^<>()\\]*$")  # nothing that could call or load (url(…), expression(…))
# Transforms: geometric functions of numbers only ("rotate(-30 120 80) translate(5,0)")
TRANSFORM = re.compile(r"^(\s*(matrix|translate|scale|rotate|skewX|skewY)\(\s*[-+\d.eE\s,]*\)\s*,?)+\s*$")


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _safe_value(name: str, value: str) -> bool:
    if name.startswith("marker-"):
        return bool(MARKER_REF.match(value.strip())) or value.strip() == "none"
    if name == "transform":
        return bool(TRANSFORM.match(value))
    return bool(SAFE_VALUE.match(value)) and "javascript" not in value.lower()


def _clean_style(style: str) -> str:
    kept = []
    for declaration in style.split(";"):
        name, _, value = declaration.partition(":")
        name, value = name.strip().lower(), value.strip()
        if name in PRESENTATION and value and _safe_value(name, value):
            kept.append(f"{name}:{value}")
    return ";".join(kept)


def _clean(element: ElementTree.Element) -> None:
    for child in list(element):
        if not isinstance(child.tag, str) or _local(child.tag) not in ELEMENTS:
            element.remove(child)  # script, foreignObject, image, a, use, animate…, comments
        else:
            _clean(child)
    for name in list(element.attrib):
        local, value = _local(name), element.attrib[name]
        if local == "style":
            cleaned = _clean_style(value)
            if cleaned:
                element.attrib[name] = cleaned
            else:
                del element.attrib[name]
        elif local not in GEOMETRY | PRESENTATION or not _safe_value(local, value):
            del element.attrib[name]  # on…, href, xlink:href, anything unknown or unsafe


def clean(svg: str) -> str:
    """The SVG with only shapes, text and their presentation: nothing that runs (scripts,
    event handlers), loads (links, images, external references) or expands (no DTD).
    Sized from its viewBox, so an <img> shows it at its size and scales it down."""
    svg = svg.strip()
    if len(svg) > MAX_SIZE:
        raise FigureError("figure.invalid")
    if re.search(r"<!(DOCTYPE|ENTITY)", svg, re.IGNORECASE):
        raise FigureError("figure.invalid")  # no DTD: no entities to expand or fetch
    try:
        root = ElementTree.fromstring(svg)
    except ElementTree.ParseError as e:
        raise FigureError("figure.invalid") from e
    if _local(root.tag) != "svg":
        raise FigureError("figure.invalid")
    if root.tag != f"{{{SVG_NS}}}svg":  # written without its namespace: give it one
        for element in root.iter():
            if isinstance(element.tag, str) and not element.tag.startswith("{"):
                element.tag = f"{{{SVG_NS}}}{element.tag}"
    _clean(root)
    viewbox = root.get("viewBox") or VIEWBOX
    root.set("viewBox", viewbox)
    try:
        _, _, width, height = (float(v) for v in re.split(r"[\s,]+", viewbox.strip()))
    except ValueError as e:
        raise FigureError("figure.invalid") from e
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")
    return ElementTree.tostring(root, encoding="unicode")


def save(folder: Path, card: Card, svg: str) -> str:
    """The cleaned figure in the lesson's images/ folder; returns its file name (a card
    picture: shown, exported and pruned like the others)."""
    data = clean(svg).encode()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"picture-{card.id}-{hashlib.sha1(data).hexdigest()[:8]}.svg"
    (folder / name).write_bytes(data)
    return name
