"""Helpers for CapCut-like ASS shadow effects.

ASS styles only expose a single diagonal shadow value. VideoCaptioner stores the
extra blur/distance/angle values in ignored comment lines and expands them into
libass override tags on each event.
"""

import math
import re
from typing import Dict, List, Tuple


# Jianying's shadow distance uses its own canvas-relative unit.  On the
# 1080x1920 portrait canvas calibrated for this patch, Jianying distance 5
# matches roughly ASS distance 2 on VideoCaptioner's 1280x720 script canvas.
CAPCUT_SHADOW_DISTANCE_TO_ASS = 0.4

# Exported-frame calibration: Jianying blur 15 matches libass blur 2.4 once the
# hard outline is reduced.  This produces Jianying's softer outer transition
# without changing the font size or glyph geometry.
CAPCUT_SHADOW_BLUR_TO_ASS = 2.4 / 15.0

# V7 outline calibration used both for new styles and older saved presets.
CAPCUT_OUTLINE_TO_ASS = 0.2

# Explicit per-style opt-in. Unmarked styles keep the legacy event stream.
SOLID_SHADOW_SUPPORTED = True
SOLID_SHADOW_PREFIX = '; VideoCaptionerSolidShadow: '


EFFECT_RE = re.compile(
    r"^;\s*VideoCaptionerEffect:\s*([^,]+),\s*"
    r"blur=([-+]?\d+(?:\.\d+)?),\s*"
    r"distance=([-+]?\d+(?:\.\d+)?),\s*"
    r"angle=([-+]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
OUTLINE_RE = re.compile(
    r"^;\s*VideoCaptionerOutline:\s*([^,]+),\s*"
    r"width=([-+]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def _number(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def normalize_capcut_style_bold(style_str: str) -> str:
    """Apply current weight and outline calibration to new or older presets.

    Existing style files created by earlier patch versions contain ``Bold=-1``.
    Jianying's bundled system font is already a Medium face and its reference
    project uses ``bold_width=0``, so forcing ASS bold makes both the glyph and
    its outline too heavy.  V3-V5 presets also store the original Jianying
    outline value in a comment; use that source value to apply the newer V7
    0.2 conversion at export time without rewriting the user's style files.
    """
    outline_widths: Dict[str, float] = {}
    for line in style_str.splitlines():
        match = OUTLINE_RE.match(line.strip())
        if match:
            outline_widths[match.group(1).strip()] = max(
                0.0, min(100.0, float(match.group(2)))
            )

    explicit_bold = {m.group(1).strip(): m.group(2) == '1'
                     for m in re.finditer(r'^; VideoCaptionerBold: ([^,]+),([01])\s*$', style_str, re.MULTILINE)}
    normalized_lines = []
    for line in style_str.splitlines():
        if line.lstrip().lower().startswith("style:"):
            fields = line.split(",")
            # ASS V4+ style field 8 is Bold (zero-based index 7).
            if len(fields) > 7:
                name = fields[0].split(':', 1)[1].strip()
                fields[7] = '-1' if explicit_bold.get(name, False) else '0'
            # ASS V4+ field 17 is Outline (zero-based index 16).
            style_name = fields[0].split(":", 1)[1].strip()
            if len(fields) > 16 and style_name in outline_widths:
                fields[16] = _number(
                    outline_widths[style_name] * CAPCUT_OUTLINE_TO_ASS
                )
            if len(fields) > 7:
                line = ",".join(fields)
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def parse_effects(style_str: str) -> Dict[str, Dict[str, float]]:
    effects: Dict[str, Dict[str, float]] = {}
    for line in style_str.splitlines():
        match = EFFECT_RE.match(line.strip())
        if match:
            effects[match.group(1).strip()] = {
                "blur": max(0.0, min(100.0, float(match.group(2)))),
                "distance": max(0.0, min(100.0, float(match.group(3)))),
                "angle": max(-180.0, min(180.0, float(match.group(4)))),
            }
    return effects


def _parse_style_visuals(style_str: str) -> Dict[str, Dict[str, float]]:
    """Return the ASS outline size and alpha for each named style."""
    styles: Dict[str, Dict[str, float]] = {}
    for line in style_str.splitlines():
        if not line.lstrip().lower().startswith("style:"):
            continue
        fields = line.split(",")
        if len(fields) <= 16:
            continue
        name = fields[0].split(":", 1)[1].strip()
        try:
            outline = max(0.0, float(fields[16]))
        except ValueError:
            outline = 0.0

        colour = fields[5].strip().upper().replace("&H", "").rstrip("&")
        try:
            outline_alpha = int(colour.zfill(8)[:2], 16)
        except ValueError:
            outline_alpha = 0
        styles[name] = {
            "outline": outline,
            "outline_alpha": float(outline_alpha),
        }
    return styles


def _scaled_ass_alpha(original_alpha: float, opacity_scale: float) -> int:
    """Scale ASS opacity while preserving the user's outline transparency."""
    original_opacity = 1.0 - max(0.0, min(255.0, original_alpha)) / 255.0
    scaled_opacity = original_opacity * max(0.0, min(1.0, opacity_scale))
    return int(round(255.0 * (1.0 - scaled_opacity)))


def solid_shadow_styles(style_str: str) -> Dict[str, bool]:
    modes: Dict[str, bool] = {}
    for line in style_str.splitlines():
        if not line.startswith(SOLID_SHADOW_PREFIX):
            continue
        match = re.fullmatch(r'; VideoCaptionerSolidShadow: (Default|Secondary),([01])', line)
        if not match or match.group(1) in modes:
            raise ValueError('实体投影标记无效或重复')
        modes[match.group(1)] = match.group(2) == '1'
    return modes


def _solid_shadow_face(style_str: str, style_name: str) -> str:
    for line in style_str.splitlines():
        if not line.startswith('Style: '):
            continue
        fields = line.split(',')
        if fields[0].split(':', 1)[1].strip() != style_name:
            continue
        colour = fields[6].strip().upper().removeprefix('&H').removesuffix('&')
        if not re.fullmatch(r'[0-9A-F]{6}|[0-9A-F]{8}', colour):
            raise ValueError('实体投影颜色无效')
        colour = colour.zfill(8)
        # Give the shadow event a complete glyph silhouette. The independent
        # white face overlays its unshifted portion, leaving the native shadow
        # visible with the configured BackColour, alpha, blur and offsets.
        return r'\1c&H' + colour[2:] + r'&\1a&H' + colour[:2] + '&'
    raise ValueError('实体投影缺少对应字幕样式')


def effect_text_layers(
    style_str: str, style_name: str, text: str
) -> List[Tuple[int, str]]:
    """Return Jianying-like, independently composited ASS text layers.

    Jianying renders the fill, core stroke, feathered outer edge and directional
    shadow separately.  A single libass event cannot reproduce that: increasing
    blur softens the white fill while leaving an opaque dark core.  V10 therefore
    emits a shadow-only layer, a broad outer feather, a softly blurred inner
    stroke and a clean top layer.
    """
    solid_shadow = solid_shadow_styles(style_str).get(style_name, False)
    effect = parse_effects(style_str).get(style_name)
    if effect is None:
        return [(0, text)]

    visual = _parse_style_visuals(style_str).get(
        style_name, {"outline": 0.0, "outline_alpha": 0.0}
    )
    core_outline = visual["outline"]
    blur = effect["blur"] * CAPCUT_SHADOW_BLUR_TO_ASS
    capcut_distance = effect["distance"]
    distance = capcut_distance * CAPCUT_SHADOW_DISTANCE_TO_ASS
    radians = math.radians(effect["angle"])
    x_offset = math.cos(radians) * distance
    # Screen Y grows downward.  This makes -45 degrees point down-right,
    # matching the angle convention used by common Chinese video editors.
    y_offset = -math.sin(radians) * distance
    layers: List[Tuple[int, str]] = []

    if capcut_distance > 0:
        # Hide fill and border but keep BackColour-driven shadow visible.
        shadow_face = _solid_shadow_face(style_str, style_name) if solid_shadow else r'\1a&HFF&'
        shadow_tags = (
            '{' + shadow_face + r"\3a&HFF&"
            r"\shad1"
            f"\\blur{_number(blur)}"
            f"\\xshad{_number(x_offset)}"
            f"\\yshad{_number(y_offset)}}}"
        )
        layers.append((0, shadow_tags + text))

    if core_outline > 0:
        # The outer fringe is intentionally broad, translucent and visibly
        # blurred.  V9 used a comparatively opaque blur=0.7 layer; at export
        # resolution that still looked like a solid outline instead of the
        # gradual dark-to-transparent falloff produced by Jianying.
        outer_outline = core_outline * 2.4
        outer_blur = max(1.2, blur * 0.65)
        outer_alpha = _scaled_ass_alpha(visual["outline_alpha"], 0.5)
        outer_tags = (
            r"{\1a&HFF&"
            f"\\3a&H{outer_alpha:02X}&"
            r"\4a&HFF&"
            f"\\bord{_number(outer_outline)}"
            r"\shad0"
            f"\\blur{_number(outer_blur)}}}"
        )
        layers.append((1, outer_tags + text))

        # A second, narrower stroke restores the dark definition that was lost
        # when V9 removed the opaque core.  It is still semi-transparent and
        # blurred, so supersampling cannot collapse it into a hard pixel ring.
        inner_outline = core_outline * 1.05
        inner_blur = max(0.45, blur * 0.22)
        inner_alpha = _scaled_ass_alpha(visual["outline_alpha"], 0.9)
        inner_tags = (
            r"{\1a&HFF&"
            f"\\3a&H{inner_alpha:02X}&"
            r"\4a&HFF&"
            f"\\bord{_number(inner_outline)}"
            r"\shad0"
            f"\\blur{_number(inner_blur)}}}"
        )
        layers.append((2, inner_tags + text))

    # Keep the white face independent from both outline layers.  This preserves
    # clean glyph interiors while the two black layers control edge definition.
    main_tags = r"{\bord0\shad0\blur0.2}"
    layers.append((3, main_tags + text))
    return layers
