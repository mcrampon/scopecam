"""Rectangles and monitor layout, in logical (scaled) pixels of the desktop."""

from dataclasses import dataclass

from gi.repository import Gdk


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_points(cls, x1, y1, x2, y2):
        return cls(round(min(x1, x2)), round(min(y1, y2)), round(abs(x2 - x1)), round(abs(y2 - y1)))

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def intersect(self, other):
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2 = min(self.x + self.w, other.x + other.w)
        y2 = min(self.y + self.h, other.y + other.h)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def union(self, other):
        x1, y1 = min(self.x, other.x), min(self.y, other.y)
        x2 = max(self.x + self.w, other.x + other.w)
        y2 = max(self.y + self.h, other.y + other.h)
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def as_list(self):
        return [self.x, self.y, self.w, self.h]


@dataclass(frozen=True)
class Monitor:
    connector: str
    rect: Rect
    scale: float
    gdk: object = None


def monitors():
    display = Gdk.Display.get_default()
    result = []
    model = display.get_monitors()
    for i in range(model.get_n_items()):
        mon = model.get_item(i)
        g = mon.get_geometry()
        result.append(Monitor(mon.get_connector() or f"monitor-{i}", Rect(g.x, g.y, g.width, g.height),
                              mon.get_scale(), mon))
    return result


def desktop_rect(mons=None):
    mons = mons or monitors()
    rect = mons[0].rect
    for m in mons[1:]:
        rect = rect.union(m.rect)
    return rect


def primary_connector(bus):
    """Connector of the primary monitor (GNOME only; None elsewhere)."""
    from gi.repository import Gio, GLib

    try:
        res = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                            "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
                            Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return None
    _serial, _monitors, logical, _props = res.unpack()
    for _x, _y, _scale, _transform, primary, mons, _p in logical:
        if primary and mons:
            return mons[0][0]
    return None
