"""Design tokens for the desktop UI, derived from the Android client (REQ-039).

The Windows UI must read as the same product as the phone: identical palette,
4 dp spacing grid, the same radii and the same typographic relationships.
Tokens are expressed in density-independent units (dp on Android) and converted
to device pixels with the Tk scaling factor, so proportions survive DPI changes
instead of copying the phone's physical pixels.

Colors are the ones already validated against the Android reference screenshot
(``.harness/evidence/design/android025-reference-top.png``); shapes, spacing and
type scale are the contract that the ttk theme could not express.
"""

from __future__ import annotations

# Semantic palette shared with Android (MainActivity.java / styles.xml).
PALETTE = {
    "background": "#101815",
    "surface": "#1B2922",
    "control": "#24392E",
    "hover": "#304C3D",
    "border": "#365144",
    "text": "#EBF5F0",
    "muted": "#A4B9AE",
    "accent": "#80E8BA",
    "accent_hover": "#A0F3D0",
    "accent_pressed": "#63CFA2",
    "accent_text": "#102019",
    "disabled": "#23342B",
    "disabled_text": "#83988B",
}

FONT_FAMILY = "Segoe UI"

# Corner radii in dp (Android reference: cards 20, buttons 16).
RADIUS = {"card": 20, "control": 16, "chip": 12}

# Four-pixel grid; spacing names follow the Android layout density.
SPACING = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "xxl": 24, "xxxl": 32}

# Type scale in dp-equivalent units (Android reference: title 28, card title 18,
# status 19-20, body/notes 13-15, button 16).
TYPE_SCALE = {
    "eyebrow": 13,
    "title": 28,
    "subtitle": 16,
    "card_title": 18,
    "status": 20,
    "body": 14,
    "note": 13,
    "button": 16,
    "code": 26,
}

# Control metrics from the Android reference.
CONTROL = {
    "button_min_height": 52,
    "card_padding": 20,
    "card_gap": 16,
    "border_width": 1,
    "focus_ring": 2,
    "field_height": 44,
    "check_box": 20,
    "check_height": 30,
}


def scaling(window) -> float:
    """Tk scaling as a multiplier for dp values (1.0 at 96 dpi / 100 %)."""
    return max(1.0, float(window.tk.call("tk", "scaling")) / (96 / 72))


def dp(window, value: float) -> int:
    """Density-independent token to device pixels for this window."""
    return round(value * scaling(window))


def font_px(window, key: str) -> int:
    """Point height for a Tk font, in device pixels (Tk negative sizes are pixels)."""
    return dp(window, TYPE_SCALE[key])


def font(window, key: str, *, weight: str = "normal") -> tuple:
    """Tk font tuple from the shared type scale; negative size means pixels."""
    return (FONT_FAMILY, -font_px(window, key), weight)
