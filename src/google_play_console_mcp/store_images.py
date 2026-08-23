"""Play store-listing image rules, checked before upload.

Google Play Console MCP does not make screenshots. Generating and compositing them is a real
design problem with real tools — ``appscreens`` among them. What Google Play Console MCP owns
is the other half: **does this file actually meet Google Play's rules**, checked
locally before an upload that would otherwise 400 after a long render.

The rules have an asymmetry that catches people out, and it is the reason this
module exists rather than trusting the API to complain:

* **Screenshots and the feature graphic must NOT have an alpha channel** —
  24-bit PNG or JPEG.
* **The icon MUST have one** — 32-bit PNG.

Any exporter defaulting to "PNG" produces RGBA, which satisfies the icon and is
rejected for everything else. Discovering that after baking sixty images is the
failure this prevents.

Dimensions are read by parsing the PNG and JPEG headers directly rather than
pulling in an imaging library. It is about sixty lines, it cannot be defeated by
a wrong file extension, and it keeps a native dependency out of a server whose
other jobs are HTTP and CSV.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google_play_console_mcp.errors import ValidationError

#: Screenshot types Play accepts. Each is a separate set with its own count.
SCREENSHOT_TYPES = (
    "phoneScreenshots",
    "sevenInchScreenshots",
    "tenInchScreenshots",
    "tvScreenshots",
    "wearScreenshots",
)

#: Play requires at least two screenshots per type per locale, and caps at eight.
#: The minimum is the half people miss: a single screenshot blocks the release.
MIN_SCREENSHOTS = 2
MAX_SCREENSHOTS = 8

MIN_SCREENSHOT_SIDE = 320
MAX_SCREENSHOT_SIDE = 3840
#: The long side may be at most twice the short side.
MAX_ASPECT_RATIO = 2.0


@dataclass(frozen=True)
class ImageSpec:
    """Play's requirements for one image type."""

    image_type: str
    formats: tuple[str, ...]
    alpha: bool | None
    """True = must have alpha, False = must not, None = either is accepted."""
    exact_size: tuple[int, int] | None = None
    min_side: int | None = None
    max_side: int | None = None
    max_aspect: float | None = None
    max_bytes: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, Any]:
        """Render the spec for the list tool."""
        return {
            "image_type": self.image_type,
            "formats": list(self.formats),
            "alpha": (
                "required" if self.alpha else "forbidden" if self.alpha is False else "either"
            ),
            "exact_size": list(self.exact_size) if self.exact_size else None,
            "min_side": self.min_side,
            "max_side": self.max_side,
            "max_aspect": self.max_aspect,
            "max_bytes": self.max_bytes,
            "notes": list(self.notes),
        }


_SCREENSHOT_SPEC_ARGS: dict[str, Any] = {
    "formats": ("png", "jpeg"),
    "alpha": False,
    "min_side": MIN_SCREENSHOT_SIDE,
    "max_side": MAX_SCREENSHOT_SIDE,
    "max_aspect": MAX_ASPECT_RATIO,
    "max_bytes": 8 * 1024 * 1024,
    "notes": (
        "24-bit PNG or JPEG. An alpha channel is rejected.",
        f"Play requires {MIN_SCREENSHOTS}-{MAX_SCREENSHOTS} per type per locale.",
    ),
}

SPECS: dict[str, ImageSpec] = {
    **{
        name: ImageSpec(image_type=name, **_SCREENSHOT_SPEC_ARGS) for name in SCREENSHOT_TYPES
    },
    "icon": ImageSpec(
        image_type="icon",
        formats=("png",),
        alpha=True,
        exact_size=(512, 512),
        max_bytes=1024 * 1024,
        notes=(
            "32-bit PNG WITH an alpha channel — the opposite of every other type.",
            "Exactly 512x512.",
        ),
    ),
    "featureGraphic": ImageSpec(
        image_type="featureGraphic",
        formats=("png", "jpeg"),
        alpha=False,
        exact_size=(1024, 500),
        max_bytes=15 * 1024 * 1024,
        notes=("Exactly 1024x500. 24-bit PNG or JPEG, no alpha.",),
    ),
    "tvBanner": ImageSpec(
        image_type="tvBanner",
        formats=("png", "jpeg"),
        alpha=False,
        exact_size=(1280, 720),
        max_bytes=15 * 1024 * 1024,
        notes=("Exactly 1280x720. Required for an Android TV listing.",),
    ),
}


def list_image_specs() -> list[dict[str, Any]]:
    """Every image type Play accepts, with its rules."""
    return [SPECS[name].describe() for name in SPECS]


# --- Header parsing ----------------------------------------------------------


@dataclass(frozen=True)
class ImageInfo:
    """What the file's own header says it is, regardless of its extension."""

    format: str
    width: int
    height: int
    has_alpha: bool
    bit_depth: int | None
    size_bytes: int


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: PNG colour types that carry an alpha channel: 4 = grey+alpha, 6 = RGBA.
_PNG_ALPHA_COLOUR_TYPES = frozenset({4, 6})
_PNG_PALETTE_COLOUR_TYPE = 3

#: JPEG start-of-frame markers. Any of them carries the dimensions; the ones
#: excluded here are restart, and application/comment segments.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _read_png(data: bytes) -> ImageInfo:
    """Parse a PNG header.

    Alpha is not just the colour type: a palette image with a ``tRNS`` chunk is
    also transparent, and Play rejects it for a screenshot exactly as it rejects
    RGBA. Missing that would let a transparent file through the check and fail
    at upload.
    """
    if len(data) < 26:
        raise ValidationError(
            "PNG file is truncated: no IHDR header.",
            remedy="Re-export the image; the file is incomplete.",
        )
    width, height = struct.unpack(">II", data[16:24])
    bit_depth = data[24]
    colour_type = data[25]

    has_alpha = colour_type in _PNG_ALPHA_COLOUR_TYPES
    if colour_type == _PNG_PALETTE_COLOUR_TYPE and b"tRNS" in data[:4096]:
        has_alpha = True

    return ImageInfo(
        format="png",
        width=width,
        height=height,
        has_alpha=has_alpha,
        bit_depth=bit_depth,
        size_bytes=len(data),
    )


def _read_jpeg(data: bytes) -> ImageInfo:
    """Parse a JPEG header by walking segments to the start-of-frame."""
    index = 2
    length = len(data)
    while index < length - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _JPEG_SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return ImageInfo(
                format="jpeg",
                width=width,
                height=height,
                # JPEG has no alpha channel at all, which is why it is always a
                # safe screenshot format.
                has_alpha=False,
                bit_depth=data[index + 4],
                size_bytes=length,
            )
        segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment

    raise ValidationError(
        "JPEG file has no start-of-frame marker.",
        remedy="Re-export the image; the file is truncated or not really a JPEG.",
    )


def read_image_info(path: str | Path) -> ImageInfo:
    """Read format and dimensions from the file's own bytes.

    The extension is not consulted. A ``.png`` that is really a JPEG is a real
    thing an export pipeline produces, and Play reads the bytes too.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise ValidationError(
            f"No such file: {file_path}",
            remedy="Check the path. Export the image first if it has not been rendered.",
        )
    data = file_path.read_bytes()
    if data.startswith(_PNG_SIGNATURE):
        return _read_png(data)
    if data.startswith(b"\xff\xd8"):
        return _read_jpeg(data)
    raise ValidationError(
        f"{file_path.name} is neither a PNG nor a JPEG.",
        remedy=(
            "Play accepts PNG and JPEG only. WebP, AVIF and HEIC are rejected even "
            "though the store serves them."
        ),
    )


# --- Preflight ---------------------------------------------------------------


def _check_format_and_alpha(info: ImageInfo, spec: ImageSpec) -> list[str]:
    """Format and alpha-channel rules — the pair people get backwards."""
    errors: list[str] = []
    if info.format not in spec.formats:
        errors.append(
            f"Format is {info.format.upper()}; {spec.image_type} accepts "
            f"{' or '.join(f.upper() for f in spec.formats)}."
        )
    if spec.alpha is True and not info.has_alpha:
        errors.append(
            f"{spec.image_type} must be a 32-bit PNG WITH an alpha channel; this file has none."
        )
    elif spec.alpha is False and info.has_alpha:
        errors.append(
            f"{spec.image_type} must NOT have an alpha channel, but this file does. Export as "
            f"24-bit PNG or JPEG — a plain 'PNG' export is usually RGBA and is rejected."
        )
    return errors


def _check_dimensions(info: ImageInfo, spec: ImageSpec) -> list[str]:
    """Exact size, side bounds and aspect ratio."""
    errors: list[str] = []
    if spec.exact_size and (info.width, info.height) != spec.exact_size:
        want = f"{spec.exact_size[0]}x{spec.exact_size[1]}"
        errors.append(
            f"{spec.image_type} must be exactly {want}; this is {info.width}x{info.height}."
        )
    if spec.min_side and min(info.width, info.height) < spec.min_side:
        errors.append(
            f"Shortest side is {min(info.width, info.height)}px; the minimum is "
            f"{spec.min_side}px."
        )
    if spec.max_side and max(info.width, info.height) > spec.max_side:
        errors.append(
            f"Longest side is {max(info.width, info.height)}px; the maximum is {spec.max_side}px."
        )
    if spec.max_aspect and min(info.width, info.height) > 0:
        ratio = max(info.width, info.height) / min(info.width, info.height)
        if ratio > spec.max_aspect:
            errors.append(
                f"Aspect ratio is {ratio:.2f}:1; Play allows at most "
                f"{spec.max_aspect:g}:1 for screenshots."
            )
    return errors


def _check_against(info: ImageInfo, spec: ImageSpec) -> list[str]:
    """Every rule for one image, so a caller learns all of them at once."""
    errors = _check_format_and_alpha(info, spec) + _check_dimensions(info, spec)
    if spec.max_bytes and info.size_bytes > spec.max_bytes:
        errors.append(
            f"File is {info.size_bytes / 1024 / 1024:.1f} MB; the limit for "
            f"{spec.image_type} is {spec.max_bytes / 1024 / 1024:.0f} MB."
        )
    return errors


def preflight_image(path: str | Path, image_type: str) -> dict[str, Any]:
    """Check one file against Play's rules for ``image_type``.

    Returns a report rather than raising, because the useful answer is the full
    list of what is wrong — an agent that fixes one problem per round trip is
    worse than one that gets told all four.
    """
    spec = SPECS.get(image_type)
    if spec is None:
        raise ValidationError(
            f"Unknown image type {image_type!r}.",
            remedy=f"Use one of: {', '.join(SPECS)}.",
        )

    info = read_image_info(path)
    errors = _check_against(info, spec)

    return {
        "valid": not errors,
        "path": str(Path(path).expanduser()),
        "image_type": image_type,
        "detected": {
            "format": info.format,
            "width": info.width,
            "height": info.height,
            "has_alpha": info.has_alpha,
            "bit_depth": info.bit_depth,
            "size_bytes": info.size_bytes,
        },
        "errors": errors,
        "spec": spec.describe(),
    }


def preflight_screenshot_set(
    paths: list[str], image_type: str = "phoneScreenshots"
) -> dict[str, Any]:
    """Check a whole screenshot set: each file, plus the set-level count rules.

    Play requires **at least two** screenshots per type per locale and caps at
    eight. A set of one is the common failure and it is invisible until the
    release is blocked, because each individual file is perfectly valid.
    """
    if image_type not in SCREENSHOT_TYPES:
        raise ValidationError(
            f"{image_type!r} is not a screenshot type, so set rules do not apply.",
            remedy=f"Use one of: {', '.join(SCREENSHOT_TYPES)}.",
        )

    per_image = [preflight_image(path, image_type) for path in paths]
    errors: list[str] = []

    if len(paths) < MIN_SCREENSHOTS:
        errors.append(
            f"Only {len(paths)} screenshot(s). Play requires at least {MIN_SCREENSHOTS} per "
            f"type per locale — a set of one blocks the release even though each file is valid."
        )
    if len(paths) > MAX_SCREENSHOTS:
        errors.append(
            f"{len(paths)} screenshots. Play accepts at most {MAX_SCREENSHOTS} per type per "
            f"locale; the rest are ignored or rejected."
        )

    invalid = [r for r in per_image if not r["valid"]]
    return {
        "valid": not errors and not invalid,
        "image_type": image_type,
        "count": len(paths),
        "set_errors": errors,
        "invalid_images": invalid,
        "images": per_image,
    }
