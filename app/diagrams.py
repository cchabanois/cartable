"""Diagram labels hidden on the photo (image occlusion).

The AI finds each label of a diagram and its box on the photo (in the format the
model knows best); the boxes are saved as fractions of the photo's size and the
review lets the user move them. In Anki, every card of a diagram shares one image
of it; the masks are HTML over it: on the front every label hidden behind its
number, the asked one highlighted; on the back that one shown again.
"""

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps

from .models import Card, Mask, TextLine

MAX_SIDE = 1568  # larger images get downsized by some models: pixel boxes refer to what they see
CARD_SIDE = 1000  # the diagram in Anki: enough to read it, light to sync
CARD_QUALITY = 75
PADDING = 0.006  # added around a detected box, as a fraction of the photo's larger side

# How each model family gives boxes. Gemini and Qwen are trained on 0-1000 boxes;
# for the others, pixels of the image as sent work best.
FORMATS = {
    "gemini": "[y_min, x_min, y_max, x_max] normalized to 0-1000 (box_2d)",
    "normalized": "[x_min, y_min, x_max, y_max] normalized to 0-1000",
    "pixels": "[x_min, y_min, x_max, y_max] in pixels of the photo (sizes below)",
}


def box_format(model: str) -> str:
    model = model.lower()
    if "gemini" in model:
        return "gemini"
    if "qwen" in model:
        return "normalized"
    return "pixels"


def prepare(data: bytes, media_type: str) -> tuple[bytes, str, tuple[int, int] | None]:
    """A photo as sent to the AI: upright, at most MAX_SIDE. Returns JPEG bytes,
    their media type and the size the boxes refer to (None: unreadable, sent as is)."""
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (OSError, ValueError):
        return data, media_type, None
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg", image.size


def _fractions(box: list[float], size: tuple[int, int], fmt: str) -> list[float] | None:
    """A box from the AI as [x0, y0, x1, y1] fractions of the photo; None if it isn't a box."""
    if len(box) != 4:
        return None
    width, height = size
    a, b, c, d = box
    if fmt == "gemini":
        x0, y0, x1, y1 = b / 1000, a / 1000, d / 1000, c / 1000
    elif fmt == "normalized":
        x0, y0, x1, y1 = a / 1000, b / 1000, c / 1000, d / 1000
    else:
        x0, y0, x1, y1 = a / width, b / height, c / width, d / height
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


def normalize(cards: list[Card], sizes: list[tuple[int, int] | None], fmt: str) -> None:
    """Turn the AI's boxes into fractions of the photo, with a little margin. A mask
    that can't be placed (unknown photo, empty box) is dropped: the card stays text."""
    for card in cards:
        mask = card.mask
        if mask is None:
            continue
        size = sizes[mask.page - 1] if 1 <= mask.page <= len(sizes) else None
        box = _fractions(mask.box, size, fmt) if size else None
        if box is None:
            card.mask = None
            continue
        width, height = size
        x0, y0, x1, y1 = box
        pad_x = PADDING * max(width, height) / width
        pad_y = PADDING * max(width, height) / height
        card.mask = Mask(page=mask.page, n=mask.n, box=_clamp([x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y]))


def turns(lines: list[TextLine], sizes: list[tuple[int, int] | None], fmt: str) -> list[int]:
    """The clockwise turn that puts each photo upright, from the reading direction of
    a line of text on it: from its first word to its last. Text going down the photo
    means its top is on the right (270°), going up: on the left (90°), going left:
    upside down (180°). Unknown: 0."""
    result = [0] * len(sizes)
    for line in lines:
        size = sizes[line.page - 1] if 1 <= line.page <= len(sizes) else None
        first = _fractions(line.first_word, size, fmt) if size else None
        last = _fractions(line.last_word, size, fmt) if size else None
        if first is None or last is None:
            continue
        width, height = size
        dx = ((last[0] + last[2]) - (first[0] + first[2])) / 2 * width
        dy = ((last[1] + last[3]) - (first[1] + first[3])) / 2 * height
        if abs(dx) >= abs(dy):
            result[line.page - 1] = 0 if dx >= 0 else 180
        else:
            result[line.page - 1] = 270 if dy > 0 else 90
    return result


ROTATIONS = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def rotate_box(box: list[float], degrees: int) -> list[float]:
    """A box (fractions of the photo) once the photo is turned `degrees` clockwise."""
    x0, y0, x1, y1 = box
    if degrees == 90:
        return _clamp([1 - y1, x0, 1 - y0, x1])
    if degrees == 180:
        return _clamp([1 - x1, 1 - y1, 1 - x0, 1 - y0])
    if degrees == 270:
        return _clamp([y0, 1 - x1, y1, 1 - x0])
    return box


def turn(data: bytes, masks: list[Mask], degrees: int) -> bytes:
    """A photo turned `degrees` clockwise, its masks turned with it (in place).
    Not a right angle, or an unreadable photo: unchanged."""
    if degrees not in ROTATIONS:
        return data
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (OSError, ValueError):
        return data
    out = io.BytesIO()
    image.transpose(ROTATIONS[degrees]).save(out, "JPEG", quality=90)
    for mask in masks:
        mask.box = rotate_box(mask.box, degrees)
    return out.getvalue()


def straighten(photos: list[bytes], cards: list[Card], rotations: list[int]) -> list[bytes]:
    """Turn the photos the AI found sideways or upside down, and their masks with them."""
    return [
        turn(data, [c.mask for c in cards if c.mask and c.mask.page == page], rotations[page - 1])
        if page <= len(rotations)
        else data
        for page, data in enumerate(photos, start=1)
    ]


def _clamp(box: list[float]) -> list[float]:
    return [round(min(1.0, max(0.0, v)), 4) for v in box]


def page_image(folder: Path, photo: Path) -> Path:
    """The photo as sent to Anki, shared by every card of the diagram: light (the
    masks are HTML over it, not drawn in). Named after its content."""
    data = photo.read_bytes()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"diagram-{photo.stem}-{hashlib.sha1(data).hexdigest()[:10]}.jpg"
    if not path.exists():
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        image.thumbnail((CARD_SIDE, CARD_SIDE))
        image.save(path, "JPEG", quality=CARD_QUALITY, optimize=True)
    return path


def masks_html(masks: list[Mask], target: int, reveal: bool) -> str:
    """The masks over the diagram, positioned in % of the image: every label hidden
    behind its number, `target` highlighted (question) or shown again (answer)."""
    parts = []
    for mask in sorted(masks, key=lambda m: m.n):
        x0, y0, x1, y1 = (round(v * 100, 2) for v in mask.box)
        style = f"left:{x0}%;top:{y0}%;width:{round(x1 - x0, 2)}%;height:{round(y1 - y0, 2)}%"
        if mask.n == target:
            kind, text = ("revealed", "") if reveal else ("target", f"({mask.n})")
        else:
            kind, text = "", f"({mask.n})"
        classes = " ".join(filter(None, ("cartable-mask", kind)))
        parts.append(f'<div class="{classes}" style="{style}">{text}</div>')
    return "".join(parts)


def prune(folder: Path, keep: set[Path]) -> None:
    """Remove the images no card uses any more (photos deleted, older versions)."""
    if folder.is_dir():
        for path in folder.glob("diagram-*.jpg"):
            if path not in keep:
                path.unlink(missing_ok=True)
