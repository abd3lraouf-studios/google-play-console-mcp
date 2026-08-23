"""Tests for store-image preflight: header parsing and Play's rules."""

from __future__ import annotations

import struct
import zlib

import pytest

import google_play_console_mcp.server as server
from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.store_images import (
    MAX_SCREENSHOTS,
    MIN_SCREENSHOTS,
    SCREENSHOT_TYPES,
    SPECS,
    list_image_specs,
    preflight_image,
    preflight_screenshot_set,
    read_image_info,
)

# --- Fixture builders --------------------------------------------------------
#
# Real PNG and JPEG bytes rather than mocks: the whole point of this module is
# that it reads headers, so a mocked reader would test nothing.

#: PNG colour types.
GREY, RGB, PALETTE, GREY_ALPHA, RGBA = 0, 2, 3, 4, 6


def png_bytes(
    width: int,
    height: int,
    colour_type: int = RGB,
    bit_depth: int = 8,
    *,
    trns: bool = False,
    pad: int = 0,
) -> bytes:
    """Build a structurally valid PNG with the given IHDR."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if trns:
        out += chunk(b"tRNS", b"\x00")
    out += chunk(b"IDAT", zlib.compress(b"\x00" * 16))
    if pad:
        out += chunk(b"teXt", b"x" * pad)
    return out + chunk(b"IEND", b"")


def jpeg_bytes(width: int, height: int, *, pad: int = 0) -> bytes:
    """Build a minimal JPEG carrying a real SOF0 segment."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    filler = b"\xff\xfe" + struct.pack(">H", pad + 2) + b"x" * pad if pad else b""
    return b"\xff\xd8" + filler + sof + b"\xff\xd9"


def write(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# --- Header parsing ----------------------------------------------------------


def test_reads_png_dimensions_and_alpha(tmp_path):
    info = read_image_info(write(tmp_path, "a.png", png_bytes(1080, 1920, RGBA)))
    assert (info.format, info.width, info.height) == ("png", 1080, 1920)
    assert info.has_alpha is True


@pytest.mark.parametrize(
    ("colour_type", "expected"),
    [(GREY, False), (RGB, False), (PALETTE, False), (GREY_ALPHA, True), (RGBA, True)],
)
def test_alpha_is_read_from_the_png_colour_type(tmp_path, colour_type, expected):
    info = read_image_info(write(tmp_path, "a.png", png_bytes(512, 512, colour_type)))
    assert info.has_alpha is expected


def test_a_palette_png_with_trns_counts_as_transparent(tmp_path):
    """Play rejects it exactly as it rejects RGBA; colour type alone would miss it."""
    path = write(tmp_path, "a.png", png_bytes(512, 512, PALETTE, trns=True))
    assert read_image_info(path).has_alpha is True


def test_reads_jpeg_dimensions_by_walking_to_the_sof(tmp_path):
    info = read_image_info(write(tmp_path, "a.jpg", jpeg_bytes(1080, 1920, pad=64)))
    assert (info.format, info.width, info.height) == ("jpeg", 1080, 1920)
    assert info.has_alpha is False  # JPEG has no alpha channel at all


def test_format_comes_from_the_bytes_not_the_extension(tmp_path):
    """An export pipeline emitting a JPEG named .png is a real thing."""
    assert read_image_info(write(tmp_path, "lying.png", jpeg_bytes(800, 600))).format == "jpeg"


def test_missing_file_says_so(tmp_path):
    with pytest.raises(ValidationError, match="No such file"):
        read_image_info(tmp_path / "nope.png")


def test_a_non_image_is_rejected_naming_the_accepted_formats(tmp_path):
    with pytest.raises(ValidationError, match="neither a PNG nor a JPEG") as exc:
        read_image_info(write(tmp_path, "a.webp", b"RIFF____WEBPVP8 "))
    assert "WebP" in exc.value.remedy


def test_a_truncated_png_is_reported(tmp_path):
    with pytest.raises(ValidationError, match="truncated"):
        read_image_info(write(tmp_path, "a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4))


def test_a_jpeg_without_a_sof_is_reported(tmp_path):
    with pytest.raises(ValidationError, match="no start-of-frame"):
        read_image_info(write(tmp_path, "a.jpg", b"\xff\xd8" + b"\x00" * 64))


# --- The alpha trap, both directions -----------------------------------------


def test_an_rgba_screenshot_is_rejected(tmp_path):
    """The failure this module exists to prevent: a plain PNG export is RGBA."""
    result = preflight_image(write(tmp_path, "s.png", png_bytes(1080, 1920, RGBA)), "phoneScreenshots")
    assert result["valid"] is False
    assert "must NOT have an alpha channel" in result["errors"][0]


def test_a_24_bit_screenshot_passes(tmp_path):
    assert preflight_image(
        write(tmp_path, "s.png", png_bytes(1080, 1920, RGB)), "phoneScreenshots"
    )["valid"]


def test_an_icon_without_alpha_is_rejected(tmp_path):
    """The same rule inverted — the icon is the one type that requires alpha."""
    result = preflight_image(write(tmp_path, "i.png", png_bytes(512, 512, RGB)), "icon")
    assert result["valid"] is False
    assert "WITH an alpha channel" in result["errors"][0]


def test_an_rgba_icon_passes(tmp_path):
    assert preflight_image(write(tmp_path, "i.png", png_bytes(512, 512, RGBA)), "icon")["valid"]


def test_a_jpeg_screenshot_passes(tmp_path):
    assert preflight_image(
        write(tmp_path, "s.jpg", jpeg_bytes(1080, 1920)), "phoneScreenshots"
    )["valid"]


def test_a_jpeg_icon_is_rejected_on_format(tmp_path):
    result = preflight_image(write(tmp_path, "i.jpg", jpeg_bytes(512, 512)), "icon")
    assert any("accepts PNG" in e for e in result["errors"])


# --- Dimensions --------------------------------------------------------------


def test_a_screenshot_below_the_minimum_side_is_rejected(tmp_path):
    result = preflight_image(write(tmp_path, "s.png", png_bytes(200, 400, RGB)), "phoneScreenshots")
    assert any("minimum is 320px" in e for e in result["errors"])


def test_a_screenshot_above_the_maximum_side_is_rejected(tmp_path):
    result = preflight_image(
        write(tmp_path, "s.png", png_bytes(4000, 4000, RGB)), "phoneScreenshots"
    )
    assert any("maximum is 3840px" in e for e in result["errors"])


def test_a_screenshot_past_two_to_one_is_rejected(tmp_path):
    result = preflight_image(write(tmp_path, "s.png", png_bytes(400, 1200, RGB)), "phoneScreenshots")
    assert any("Aspect ratio is 3.00:1" in e for e in result["errors"])


def test_exactly_two_to_one_is_accepted(tmp_path):
    assert preflight_image(
        write(tmp_path, "s.png", png_bytes(500, 1000, RGB)), "phoneScreenshots"
    )["valid"]


@pytest.mark.parametrize(
    ("image_type", "size"),
    [("icon", (512, 512)), ("featureGraphic", (1024, 500)), ("tvBanner", (1280, 720))],
)
def test_fixed_size_types_accept_their_exact_size(tmp_path, image_type, size):
    colour = RGBA if image_type == "icon" else RGB
    path = write(tmp_path, "a.png", png_bytes(*size, colour))
    assert preflight_image(path, image_type)["valid"]


def test_a_feature_graphic_at_the_wrong_size_is_rejected(tmp_path):
    result = preflight_image(write(tmp_path, "f.png", png_bytes(1200, 600, RGB)), "featureGraphic")
    assert any("must be exactly 1024x500" in e for e in result["errors"])


def test_an_oversized_icon_file_is_rejected(tmp_path):
    """1 MB is the icon cap, and a 512x512 PNG can exceed it with enough metadata."""
    path = write(tmp_path, "i.png", png_bytes(512, 512, RGBA, pad=1024 * 1024 + 1))
    result = preflight_image(path, "icon")
    assert any("the limit for icon is 1 MB" in e for e in result["errors"])


def test_every_broken_rule_is_reported_at_once(tmp_path):
    """One fix per round trip is worse than being told all of them."""
    result = preflight_image(write(tmp_path, "i.jpg", jpeg_bytes(100, 100)), "icon")
    assert len(result["errors"]) >= 2  # wrong format, no alpha, wrong size
    assert result["valid"] is False


def test_the_report_carries_what_was_detected_and_the_spec(tmp_path):
    result = preflight_image(write(tmp_path, "s.png", png_bytes(1080, 1920, RGB)), "phoneScreenshots")
    assert result["detected"]["width"] == 1080
    assert result["detected"]["has_alpha"] is False
    assert result["spec"]["alpha"] == "forbidden"


def test_an_unknown_image_type_names_the_valid_ones(tmp_path):
    with pytest.raises(ValidationError, match="Unknown image type") as exc:
        preflight_image(write(tmp_path, "a.png", png_bytes(10, 10)), "bannerOfHolding")
    assert "featureGraphic" in exc.value.remedy


# --- Set rules ---------------------------------------------------------------


def test_a_set_of_one_is_rejected_though_the_file_is_valid(tmp_path):
    """The invisible failure: every image passes, the release is still blocked."""
    path = write(tmp_path, "s.png", png_bytes(1080, 1920, RGB))
    result = preflight_screenshot_set([path])
    assert result["images"][0]["valid"] is True
    assert result["valid"] is False
    assert "at least 2" in result["set_errors"][0]


def test_a_set_of_two_passes(tmp_path):
    paths = [write(tmp_path, f"s{i}.png", png_bytes(1080, 1920, RGB)) for i in range(2)]
    result = preflight_screenshot_set(paths)
    assert result["valid"] is True
    assert result["count"] == 2


def test_a_set_above_the_cap_is_rejected(tmp_path):
    paths = [
        write(tmp_path, f"s{i}.png", png_bytes(1080, 1920, RGB)) for i in range(MAX_SCREENSHOTS + 1)
    ]
    result = preflight_screenshot_set(paths)
    assert result["valid"] is False
    assert "at most 8" in result["set_errors"][0]


def test_a_set_surfaces_the_invalid_members(tmp_path):
    good = write(tmp_path, "good.png", png_bytes(1080, 1920, RGB))
    bad = write(tmp_path, "bad.png", png_bytes(1080, 1920, RGBA))
    result = preflight_screenshot_set([good, bad])
    assert result["valid"] is False
    assert len(result["invalid_images"]) == 1


def test_set_rules_do_not_apply_to_non_screenshot_types(tmp_path):
    with pytest.raises(ValidationError, match="not a screenshot type"):
        preflight_screenshot_set([write(tmp_path, "i.png", png_bytes(512, 512, RGBA))], "icon")


# --- Specs -------------------------------------------------------------------


def test_specs_cover_every_screenshot_type_plus_the_fixed_size_ones():
    names = {s["image_type"] for s in list_image_specs()}
    assert set(SCREENSHOT_TYPES) <= names
    assert {"icon", "featureGraphic", "tvBanner"} <= names


def test_the_icon_is_the_only_type_requiring_alpha():
    requiring = [name for name, spec in SPECS.items() if spec.alpha is True]
    assert requiring == ["icon"]


def test_screenshot_count_bounds_are_googles():
    assert (MIN_SCREENSHOTS, MAX_SCREENSHOTS) == (2, 8)


# --- Server tools and prompts ------------------------------------------------


def test_tool_list_store_image_specs():
    specs = server.list_store_image_specs()
    icon = next(s for s in specs if s["image_type"] == "icon")
    assert icon["alpha"] == "required"
    assert icon["exact_size"] == [512, 512]


def test_tool_preflight_store_image(tmp_path):
    path = write(tmp_path, "s.png", png_bytes(1080, 1920, RGB))
    assert server.preflight_store_image(image_path=path, image_type="phoneScreenshots")["valid"]


def test_tool_preflight_store_image_returns_the_error_object(tmp_path):
    result = server.preflight_store_image(image_path=str(tmp_path / "gone.png"), image_type="icon")
    assert result["error"] == "validation_error"
    assert "Check the path" in result["remedy"]


def test_tool_preflight_screenshot_set(tmp_path):
    paths = [write(tmp_path, f"s{i}.png", png_bytes(1080, 1920, RGB)) for i in range(2)]
    assert server.preflight_screenshot_set(image_paths=paths)["valid"] is True


def test_tool_preflight_screenshot_set_returns_the_error_object():
    result = server.preflight_screenshot_set(image_paths=[], image_type="icon")
    assert result["error"] == "validation_error"


def test_preflight_tools_are_reads_and_work_in_read_only_mode(tmp_path):
    path = write(tmp_path, "s.png", png_bytes(1080, 1920, RGB))
    server.set_read_only(True)
    try:
        assert server.preflight_store_image(image_path=path, image_type="phoneScreenshots")[
            "valid"
        ]
    finally:
        server.set_read_only(False)


def test_the_screenshot_prompt_names_the_alpha_trap_and_the_count_rule():
    text = server.publish_screenshots(package_name="com.example.app")
    assert "NO alpha channel" in text
    assert "at least TWO" in text
    assert "preflight_screenshot_set" in text


def test_the_health_prompt_puts_freshness_first():
    text = server.release_health_check(package_name="com.example.app")
    assert text.index("get_vitals_freshness") < text.index("query_vitals")
    assert "halt_release" in text
    assert "two-step confirmed write" in text
