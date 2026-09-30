"""Diagram labels hidden on the photo (image occlusion).

The AI finds each label of a diagram and its box on the photo (in the format the
model knows best); the boxes are saved as fractions of the photo's size, the review
lets the user move them, and the card images are drawn here: on the front every
label is hidden behind its number, the asked one highlighted; on the back that one
is shown again.
"""

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import Card, Mask

MAX_SIDE = 1568  # larger images get downsized by some models: pixel boxes refer to what they see
CARD_SIDE = 1200  # card images in Anki: enough to read a diagram, light to sync
PADDING = 0.006  # added around a detected box, as a fraction of the photo's larger side

# How each model family gives boxes. Gemini and Qwen are trained on 0-1000 boxes;
# for the others, pixels of the image as sent work best.
FORMATS = {
    "gemini": "[y_min, x_min, y_max, x_max] normalized to 0-1000 (box_2d)",
    "normalized": "[x_min, y_min, x_max, y_max] normalized to 0-1000",
    "pixels": "[x_min, y_min, x_max, y_max] in pixels of the photo (sizes below)",
}

COLORS = {
    "mask": ("#ffe08a", "#c77700", "#3d2b00"),  # fill, outline, number
    "target": ("#ff7a59", "#b3261e", "#ffffff"),
    "revealed": (None, "#1b873f", None),
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


def normalize(cards: list[Card], sizes: list[tuple[int, int] | None], fmt: str) -> None:
    """Turn the AI's boxes into fractions of the photo, with a little margin. A mask
    that can't be placed (unknown photo, empty box) is dropped: the card stays text."""
    for card in cards:
        mask = card.mask
        if mask is None:
            continue
        if not 1 <= mask.page <= len(sizes) or sizes[mask.page - 1] is None or len(mask.box) != 4:
            card.mask = None
            continue
        width, height = sizes[mask.page - 1]
        a, b, c, d = mask.box
        if fmt == "gemini":
            x0, y0, x1, y1 = b / 1000, a / 1000, d / 1000, c / 1000
        elif fmt == "normalized":
            x0, y0, x1, y1 = a / 1000, b / 1000, c / 1000, d / 1000
        else:
            x0, y0, x1, y1 = a / width, b / height, c / width, d / height
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            card.mask = None
            continue
        pad_x = PADDING * max(width, height) / width
        pad_y = PADDING * max(width, height) / height
        card.mask = Mask(page=mask.page, n=mask.n, box=_clamp([x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y]))


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


def straighten(photos: list[bytes], cards: list[Card], rotations: list[int]) -> list[bytes]:
    """Turn the photos the AI found sideways or upside down, and their masks with them.
    Unknown rotations count as 0; an unreadable photo stays as it is."""
    result = []
    for page, data in enumerate(photos, start=1):
        degrees = rotations[page - 1] if page <= len(rotations) else 0
        if degrees not in ROTATIONS:
            result.append(data)
            continue
        try:
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        except (OSError, ValueError):
            result.append(data)
            continue
        out = io.BytesIO()
        image.transpose(ROTATIONS[degrees]).save(out, "JPEG", quality=90)
        result.append(out.getvalue())
        for card in cards:
            if card.mask and card.mask.page == page:
                card.mask.box = rotate_box(card.mask.box, degrees)
    return result


def _clamp(box: list[float]) -> list[float]:
    return [round(min(1.0, max(0.0, v)), 4) for v in box]


def render(photo: bytes, masks: list[Mask], target: int, reveal: bool) -> bytes:
    """The photo with every mask drawn (numbered), `target` highlighted, or shown
    again when `reveal`."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
    image.thumbnail((CARD_SIDE, CARD_SIDE))
    width, height = image.size
    draw = ImageDraw.Draw(image)
    line = max(2, round(max(width, height) / 400))
    for mask in masks:
        x0, y0, x1, y1 = mask.box[0] * width, mask.box[1] * height, mask.box[2] * width, mask.box[3] * height
        style = "revealed" if reveal and mask.n == target else "target" if mask.n == target else "mask"
        fill, outline, ink = COLORS[style]
        draw.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=line)
        if ink:
            label = f"({mask.n})"
            size = max(10, min((y1 - y0) * 0.7, (x1 - x0) / (0.6 * len(label)), max(width, height) / 25))
            font = ImageFont.load_default(size=size)
            draw.text(((x0 + x1) / 2, (y0 + y1) / 2), label, fill=ink, font=font, anchor="mm")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=82)
    return out.getvalue()


def card_images(folder: Path, photo: Path, card: Card, cards: list[Card]) -> tuple[Path, Path]:
    """Front and back images of a diagram card, in the lesson's images/ folder.
    Named after what they show, so they are only drawn again when something changed."""
    masks = sorted((c.mask for c in cards if c.mask and c.mask.page == card.mask.page), key=lambda m: m.n)
    data = photo.read_bytes()
    key = hashlib.sha1(data + repr([m.model_dump() for m in masks]).encode() + f"|{card.mask.n}".encode())
    stem = f"diagram-{card.mask.page}-{card.mask.n}-{key.hexdigest()[:10]}"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for side, reveal in (("q", False), ("a", True)):
        path = folder / f"{stem}-{side}.jpg"
        if not path.exists():
            path.write_bytes(render(data, masks, card.mask.n, reveal))
        paths.append(path)
    return paths[0], paths[1]


def prune(folder: Path, keep: set[Path]) -> None:
    """Remove the images no card uses any more (masks moved, cards deleted)."""
    if folder.is_dir():
        for path in folder.glob("diagram-*.jpg"):
            if path not in keep:
                path.unlink(missing_ok=True)
