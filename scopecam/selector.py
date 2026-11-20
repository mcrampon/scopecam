"""Fullscreen capture UI shown over a frozen image of the desktop (like GNOME's own).

One undecorated fullscreen window per monitor, all sharing one selection in desktop
(logical px) coordinates, so a region can be dragged across monitors. The monitor with
the pointer (or the primary one) also shows the control panel.
"""

import math
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_foreign("cairo")
import cairo  # noqa: E402
from gi.repository import Gdk, Gtk  # noqa: E402

from .backends.base import Target  # noqa: E402
from .geometry import Rect  # noqa: E402

HANDLE = 10  # px around edges that grab for resizing
MIN_SIZE = 4

CSS = b"""
.scopecam-selector { background: black; }
.scopecam-hint { background: alpha(black, 0.7); color: white; border-radius: 999px; padding: 8px 18px; }
"""

MODE_KEYS = {Gdk.KEY_r: "region", Gdk.KEY_s: "screen", Gdk.KEY_w: "window"}


@dataclass
class Choice:
    kind: str  # "screenshot" | "screencast"
    target: Target
    copy: bool  # Ctrl+C: copy to the clipboard regardless of the preference


class Selector:
    """on_done(Choice | None). The windows stay open after a choice until close() is called,
    so screenshots can still use our focus to copy to the clipboard; cancelling closes them.
    """

    def __init__(self, app, frames, desktop, monitors, kind, mode, on_done, windows=(), can_list_windows=True,
                 initial=None, focus=None, startup_id=None):
        self.app = app
        self.frames = frames  # {"plain": surface, "cursor": surface | None}
        self.desktop = desktop
        self.monitors = monitors
        self.kind = kind
        self.mode = mode
        self.windows = list(windows)
        self.can_list_windows = can_list_windows
        self.on_done = on_done
        self.scale = frames["plain"].get_width() / desktop.w
        self.selection = initial  # region (Rect)
        self.picked = None  # Monitor or WindowInfo, in screen/window modes
        self.pinned = False  # picked by a click: stop following the pointer
        self.drag = None  # (kind, start rect, start point)
        self.views = []
        self._finished = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # The window that gets focus and the panel: under the pointer, else the primary
        # monitor (whose top bar would otherwise stay above an unfocused fullscreen window).
        def is_main(mon):
            if isinstance(focus, tuple):
                return mon.rect.contains(*focus)
            return mon.connector == focus

        main = next((m for m in monitors if is_main(m)), monitors[0])
        self._main_monitor = main
        self._focus_point = focus if isinstance(focus, tuple) else None
        self._preselect()
        for mon in monitors:
            self.views.append(_View(self, mon, with_panel=mon is main))
        self.main_view = next(v for v in self.views if v.monitor is main)
        if startup_id:
            # Activation token from the click or shortcut that started us: lets GNOME focus us.
            self.main_view.set_startup_id(startup_id)
        for view in sorted(self.views, key=lambda v: v is self.main_view):
            view.present()
        self.refresh()

    # --- state ----------------------------------------------------------------------

    def _preselect(self):
        self.pinned = False
        if self.mode == "screen":
            pt = self._focus_point
            self.picked = next((m for m in self.monitors if pt and m.rect.contains(*pt)), None) \
                or self._main_monitor
        elif self.mode == "window":
            pt = self._focus_point
            self.picked = (next((w for w in self.windows if pt and w.rect.contains(*pt)), None)
                           or (self.windows[0] if self.windows else None))
        else:
            self.picked = None

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        self._preselect()
        self.refresh()

    def set_kind(self, kind):
        self.kind = kind
        self.refresh()

    def refresh(self):
        for v in self.views:
            v.refresh()
        self.redraw()

    def redraw(self):
        for v in self.views:
            v.area.queue_draw()

    def hint(self):
        action = "capture" if self.kind == "screenshot" else "record"
        copy = " · Ctrl+C to copy" if self.kind == "screenshot" else ""
        if self.mode == "region":
            return f"Drag to select an area · Enter to {action}{copy} · Esc to cancel"
        if self.mode == "window" and not self.can_list_windows:
            return f"Press Enter to choose a window to {action} · Esc to cancel"
        thing = "window" if self.mode == "window" else "screen"
        return f"Click a {thing} to select it · Enter to {action}{copy} · Esc to cancel"

    def target(self):
        if self.mode == "region":
            s = self.selection
            if s and s.w >= MIN_SIZE and s.h >= MIN_SIZE:
                return Target("area", s)
            return None
        if self.mode == "window" and not self.can_list_windows:
            return Target("pick-window")
        if self.picked is None:
            return None
        if self.mode == "screen":
            return Target("monitor", self.picked.rect, self.picked.connector)
        return Target("window", self.picked.rect, window_id=self.picked.id)

    def confirm(self, copy=False):
        if self._finished:
            return
        target = self.target()
        if target is None:
            return
        self._finished = True
        for v in self.views:
            v.set_cursor_from_name("wait")
        self.on_done(Choice(self.kind, target, copy))

    def cancel(self):
        if self._finished:
            return
        self._finished = True
        self.close()
        self.on_done(None)

    def close(self):
        for v in self.views:
            v.destroy()
        self.views = []

    def has_focus(self):
        return any(v.is_active() for v in self.views)

    def hit(self, x, y):
        if self.mode == "window":
            return next((w for w in self.windows if w.rect.contains(x, y)), None)
        if self.mode == "screen":
            return next((m for m in self.monitors if m.rect.contains(x, y)), None)
        return None

    def edges_at(self, x, y):
        """Which edges of the selection are under (x, y): subset of 'lrtb', 'move' or None."""
        s = self.selection
        if not s:
            return None
        if not Rect(s.x - HANDLE, s.y - HANDLE, s.w + 2 * HANDLE, s.h + 2 * HANDLE).contains(x, y):
            return None
        edges = ""
        if abs(x - s.x) <= HANDLE:
            edges += "l"
        elif abs(x - (s.x + s.w)) <= HANDLE:
            edges += "r"
        if abs(y - s.y) <= HANDLE:
            edges += "t"
        elif abs(y - (s.y + s.h)) <= HANDLE:
            edges += "b"
        if edges:
            return edges
        return "move" if s.contains(x, y) else None

    # --- input (desktop coordinates) --------------------------------------------------

    def motion(self, x, y):
        if self.mode == "region":
            return _CURSORS.get(self.edges_at(x, y), "crosshair")
        hit = self.hit(x, y)
        if not self.pinned and hit is not None and hit is not self.picked:
            self.picked = hit
            self.redraw()
        return "pointer" if hit else "default"

    def drag_begin(self, x, y):
        if self.mode != "region":
            return
        edges = self.edges_at(x, y)
        if edges:
            self.drag = (edges, self.selection, (x, y))
        else:
            self.drag = ("new", None, (x, y))
            self.selection = Rect(round(x), round(y), 0, 0)
        self.redraw()

    def drag_update(self, x, y):
        if not self.drag:
            return
        kind, start, (x0, y0) = self.drag
        d = self.desktop
        x = min(max(x, d.x), d.x + d.w)
        y = min(max(y, d.y), d.y + d.h)
        if kind == "new":
            self.selection = Rect.from_points(x0, y0, x, y)
        elif kind == "move":
            nx = min(max(start.x + x - x0, d.x), d.x + d.w - start.w)
            ny = min(max(start.y + y - y0, d.y), d.y + d.h - start.h)
            self.selection = Rect(round(nx), round(ny), start.w, start.h)
        else:
            x1, y1, x2, y2 = start.x, start.y, start.x + start.w, start.y + start.h
            if "l" in kind:
                x1 = x
            if "r" in kind:
                x2 = x
            if "t" in kind:
                y1 = y
            if "b" in kind:
                y2 = y
            self.selection = Rect.from_points(x1, y1, x2, y2)
        self.redraw()

    def drag_end(self):
        self.drag = None
        if self.selection and (self.selection.w < MIN_SIZE or self.selection.h < MIN_SIZE):
            self.selection = None
        self.redraw()

    def click(self, x, y, n_press):
        if self.mode == "region":
            if n_press == 2 and self.selection and self.selection.contains(x, y):
                self.confirm()
            return
        hit = self.hit(x, y)
        if hit is not None:
            self.picked = hit
            self.pinned = True
            self.redraw()
            if n_press == 2:
                self.confirm()

    def key(self, window, keyval, state):
        """Window-level shortcuts, run before widgets see the key."""
        if keyval == Gdk.KEY_Escape:
            self.cancel()
            return True
        if isinstance(window.get_focus(), (Gtk.Text, Gtk.Editable)):
            return False  # typing in an options field (e.g. delay)
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.confirm()
            return True
        if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if self.kind == "screenshot":
                self.confirm(copy=True)
            return True
        if ctrl:
            return False
        if keyval in MODE_KEYS:
            self.main_view.panel.set_mode(MODE_KEYS[keyval])
            return True
        if keyval == Gdk.KEY_p:
            self.main_view.panel.toggle_pointer()
            return True
        return False

    # --- drawing ----------------------------------------------------------------------

    def _frame(self):
        cfg = self.app.config
        if self.kind == "screenshot" and cfg.screenshot_pointer and self.frames.get("cursor"):
            return self.frames["cursor"]
        return self.frames["plain"]

    def draw(self, cr, mon):
        ox, oy = mon.rect.x, mon.rect.y
        cr.save()
        cr.scale(1 / self.scale, 1 / self.scale)
        cr.set_source_surface(self._frame(), -(ox - self.desktop.x) * self.scale,
                              -(oy - self.desktop.y) * self.scale)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()

        cr.translate(-ox, -oy)
        if self.mode == "region":
            hole = self.selection
        else:
            hole = self.picked.rect if self.picked else None
        # Dim everything except the selection.
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(ox, oy, mon.rect.w, mon.rect.h)
        if hole:
            cr.rectangle(hole.x, hole.y, hole.w, hole.h)
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.fill()
        if not hole:
            return
        accent = (0.21, 0.52, 0.89)
        cr.set_line_width(2)
        cr.set_source_rgb(*accent)
        cr.rectangle(hole.x + 1, hole.y + 1, hole.w - 2, hole.h - 2)
        cr.stroke()
        if self.mode == "region":
            for hx, hy in _handles(hole):
                cr.arc(hx, hy, 6, 0, 2 * math.pi)
                cr.set_source_rgb(1, 1, 1)
                cr.fill_preserve()
                cr.set_source_rgb(*accent)
                cr.set_line_width(2)
                cr.stroke()
            label = f"{hole.w} × {hole.h}"
        elif self.mode == "window":
            label = self.picked.title or self.picked.app
        else:
            label = f"{self.picked.connector} · {hole.w} × {hole.h}"
        _label(cr, label, hole)


class _View(Gtk.Window):
    def __init__(self, selector, monitor, with_panel):
        super().__init__(application=selector.app, decorated=False, title="Scopecam")
        self.selector = selector
        self.monitor = monitor
        self.add_css_class("scopecam-selector")

        overlay = Gtk.Overlay()
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(lambda _a, cr, _w, _h: selector.draw(cr, monitor))
        overlay.set_child(self.area)

        self.hint = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=24)
        self.hint.add_css_class("scopecam-hint")
        self.hint.set_can_target(False)
        overlay.add_overlay(self.hint)

        self.panel = None
        if with_panel:
            from .panel import ControlPanel

            self.panel = ControlPanel(selector)
            overlay.add_overlay(self.panel)
        self.set_child(overlay)

        ox, oy = monitor.rect.x, monitor.rect.y
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.area.set_cursor_from_name(selector.motion(x + ox, y + oy)))
        self.area.add_controller(motion)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda _g, x, y: selector.drag_begin(x + ox, y + oy))
        drag.connect("drag-update", lambda g, dx, dy: selector.drag_update(
            g.get_start_point()[1] + dx + ox, g.get_start_point()[2] + dy + oy))
        drag.connect("drag-end", lambda *_: selector.drag_end())
        self.area.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("released", lambda _g, n, x, y: selector.click(x + ox, y + oy, n))
        self.area.add_controller(click)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _c, keyval, _code, state: selector.key(self, keyval, state))
        self.add_controller(keys)

        self.connect("close-request", lambda *_: selector.cancel() or False)
        if monitor.gdk is not None:
            self.fullscreen_on_monitor(monitor.gdk)
        else:
            self.fullscreen()

    def refresh(self):
        self.hint.set_label(self.selector.hint())
        if self.panel:
            self.panel.refresh()


_CURSORS = {
    "move": "move", "l": "w-resize", "r": "e-resize", "t": "n-resize", "b": "s-resize",
    "lt": "nw-resize", "rt": "ne-resize", "lb": "sw-resize", "rb": "se-resize",
}


def _handles(r):
    xs = (r.x, r.x + r.w / 2, r.x + r.w)
    ys = (r.y, r.y + r.h / 2, r.y + r.h)
    return [(x, y) for x in xs for y in ys if (x, y) != (xs[1], ys[1])]


def _label(cr, text, rect):
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(13)
    ext = cr.text_extents(text)
    w, h = ext.x_advance + 20, 26
    x = rect.x + rect.w / 2 - w / 2
    y = rect.y - h - 8 if rect.y - h - 8 > 0 else rect.y + 8
    cr.new_path()
    cr.rectangle(x, y, w, h)
    cr.set_source_rgba(0, 0, 0, 0.75)
    cr.fill()
    cr.move_to(x + 10, y + 17)
    cr.set_source_rgb(1, 1, 1)
    cr.show_text(text)

