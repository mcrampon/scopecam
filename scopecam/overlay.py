"""Keystroke and click visualisation, drawn into the video by a cairooverlay element.

Events are stamped with the pipeline's running time; drawing uses each frame's
timestamp, so the animation stays correct even if frames are processed in bursts.
"""

import math
import threading

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, Pango, PangoCairo  # noqa: E402

SECOND = 1_000_000_000
CHIP_LIFETIME = 2 * SECOND
CHIP_FADE = SECOND // 3
TYPING_JOIN = int(1.5 * SECOND)
CLICK_LIFETIME = SECOND // 2
MAX_CHIPS = 3
MAX_TEXT = 40

MODIFIERS = {
    "KEY_LEFTCTRL": "Ctrl", "KEY_RIGHTCTRL": "Ctrl",
    "KEY_LEFTSHIFT": "Shift", "KEY_RIGHTSHIFT": "Shift",
    "KEY_LEFTALT": "Alt", "KEY_RIGHTALT": "AltGr",
    "KEY_LEFTMETA": "Super", "KEY_RIGHTMETA": "Super",
}
MOD_ORDER = ["Super", "Ctrl", "Alt", "AltGr", "Shift"]

SPECIAL = {
    "Return": "⏎", "KP_Enter": "⏎", "BackSpace": "⌫", "Tab": "⇥", "ISO_Left_Tab": "⇥",
    "Escape": "Esc", "Delete": "Del", "Insert": "Ins", "Left": "←", "Right": "→",
    "Up": "↑", "Down": "↓", "Home": "Home", "End": "End", "Page_Up": "PgUp",
    "Page_Down": "PgDn", "space": "␣", "Caps_Lock": "Caps", "Print": "PrtSc", "Menu": "Menu",
}
# Specials that read naturally inside typed text.
INLINE = {"BackSpace", "space", "Left", "Right", "Up", "Down", "Tab", "Return", "KP_Enter"}

BUTTON_COLORS = {
    "BTN_LEFT": (1.0, 0.78, 0.0),
    "BTN_RIGHT": (1.0, 0.35, 0.35),
    "BTN_MIDDLE": (0.35, 0.65, 1.0),
}


class Chip:
    def __init__(self, text, kind, t):
        self.text = text
        self.kind = kind  # "text" | "combo"
        self.count = 1
        self.t = t
        self.closed = False

    def label(self):
        return f"{self.text} ×{self.count}" if self.count > 1 else self.text


class Overlay:
    def __init__(self, show_keys=True, show_clicks=True, shortcuts_only=False):
        self.show_keys = show_keys
        self.show_clicks = show_clicks
        self.shortcuts_only = shortcuts_only
        self.lock = threading.Lock()
        self.chips = []
        self.clicks = []  # (x, y, color, t), x/y in output pixels
        self.mods = set()
        self.mod_used = False
        self.size = (0, 0)

    # --- events (main thread) -------------------------------------------------

    def key(self, code, name, pressed, t):
        if name in MODIFIERS:
            mod = MODIFIERS[name]
            if pressed:
                self.mods.add(mod)
                self.mod_used = False
            else:
                self.mods.discard(mod)
                # A modifier tapped on its own (e.g. Super to open the overview).
                if not self.mod_used and mod in ("Super", "Ctrl", "Alt") and not self.shortcuts_only:
                    self._push(mod, "combo", t)
                self.mod_used = True
            return
        if not pressed:
            return
        self.mod_used = True
        keyval_name, char = _translate(code, "Shift" in self.mods, "AltGr" in self.mods)
        chord = [m for m in MOD_ORDER if m in self.mods and m not in ("Shift", "AltGr")]
        if chord:
            base_name, base_char = _translate(code, False, False)
            key = SPECIAL.get(base_name) or (base_char.upper() if base_char else base_name or name[4:])
            shift = ["Shift"] if "Shift" in self.mods else []
            self._push("+".join(chord + shift + [key]), "combo", t)
            return
        if self.shortcuts_only:
            return
        if char and char.isprintable() and keyval_name not in SPECIAL:
            self._type(char, t)
        elif keyval_name in INLINE:
            self._type(" " if keyval_name == "space" else SPECIAL[keyval_name], t,
                       close=keyval_name in ("Return", "KP_Enter"))
        else:
            label = SPECIAL.get(keyval_name) or keyval_name or name[4:].title()
            if "Shift" in self.mods:
                label = "Shift+" + label
            self._push(label, "combo", t)

    def click(self, x, y, button, t):
        with self.lock:
            self.clicks.append((x, y, BUTTON_COLORS.get(button, (1, 1, 1)), t))
            self.clicks = self.clicks[-20:]

    def _type(self, text, t, close=False):
        with self.lock:
            last = self.chips[-1] if self.chips else None
            if last and last.kind == "text" and not last.closed and t - last.t < TYPING_JOIN:
                last.text = (last.text + text)[-MAX_TEXT:]
                last.t = t
                last.closed = close
                return
        self._push(text, "text", t, closed=close)

    def _push(self, text, kind, t, closed=False):
        with self.lock:
            last = self.chips[-1] if self.chips else None
            if last and kind == "combo" and last.kind == "combo" and last.text == text and t - last.t < CHIP_LIFETIME:
                last.count += 1
                last.t = t
                return
            chip = Chip(text, kind, t)
            chip.closed = closed
            self.chips.append(chip)
            self.chips = self.chips[-MAX_CHIPS:]

    # --- drawing (streaming thread) -------------------------------------------

    def on_caps(self, _overlay, caps):
        s = caps.get_structure(0)
        self.size = (s.get_value("width"), s.get_value("height"))

    def draw(self, _overlay, cr, pts, _duration):
        w, h = self.size
        if not w:
            return
        with self.lock:
            chips = [c for c in self.chips if c.t <= pts < c.t + CHIP_LIFETIME]
            clicks = [c for c in self.clicks if c[3] <= pts < c[3] + CLICK_LIFETIME]
        if self.show_clicks:
            for x, y, color, t in clicks:
                self._draw_click(cr, x, y, color, (pts - t) / CLICK_LIFETIME, h)
        if self.show_keys and chips:
            self._draw_chips(cr, chips, pts, w, h)

    @staticmethod
    def _draw_click(cr, x, y, color, p, h):
        unit = max(1.0, h / 1080)
        ease = 1 - (1 - p) ** 3
        r, g, b = color
        cr.new_path()
        if p < 0.35:
            cr.arc(x, y, 9 * unit, 0, 2 * math.pi)
            cr.set_source_rgba(r, g, b, 0.55 * (1 - p / 0.35))
            cr.fill()
        cr.arc(x, y, (10 + 30 * ease) * unit, 0, 2 * math.pi)
        cr.set_line_width(4 * unit)
        cr.set_source_rgba(r, g, b, 0.95 * (1 - p))
        cr.stroke()

    @staticmethod
    def _draw_chips(cr, chips, pts, w, h):
        unit = max(0.6, h / 1080)
        font = Pango.FontDescription.from_string(f"Sans Bold {round(26 * unit)}")
        pad_x, pad_y, gap = 22 * unit, 12 * unit, 10 * unit
        y = h - 60 * unit
        for chip in reversed(chips):
            age = pts - chip.t
            alpha = 1.0 if age < CHIP_LIFETIME - CHIP_FADE else (CHIP_LIFETIME - age) / CHIP_FADE
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(font)
            layout.set_text(chip.label(), -1)
            _ink, logical = layout.get_pixel_extents()
            bw, bh = logical.width + 2 * pad_x, logical.height + 2 * pad_y
            x = (w - bw) / 2
            y -= bh
            _rounded(cr, x, y, bw, bh, 12 * unit)
            cr.set_source_rgba(0.1, 0.1, 0.12, 0.82 * alpha)
            cr.fill()
            cr.move_to(x + pad_x, y + pad_y)
            cr.set_source_rgba(1, 1, 1, alpha)
            PangoCairo.show_layout(cr, layout)
            y -= gap


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _translate(evdev_code, shift, altgr):
    """Map an evdev keycode to (keyval name, character) using the user's keyboard layout."""
    display = Gdk.Display.get_default()
    if display is None:
        return None, None
    ok, keys, keyvals = display.map_keycode(evdev_code + 8)
    if not ok:
        return None, None
    level = (1 if shift else 0) + (2 if altgr else 0)
    by_level = {k.level: v for k, v in zip(keys, keyvals) if k.group == 0}
    keyval = by_level.get(level) or by_level.get(level & 1) or by_level.get(0) or keyvals[0]
    char = chr(Gdk.keyval_to_unicode(keyval) or 0)
    return Gdk.keyval_name(keyval), (char if char != "\0" else None)
