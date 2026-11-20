from dataclasses import dataclass

from ..geometry import Rect

# Cursor modes shared by Mutter and the portal (portal uses a bitmask, mapped there).
CURSOR_HIDDEN = 0
CURSOR_EMBEDDED = 1


@dataclass
class Target:
    """What to capture.

    kind: "area" (rect), "monitor" (connector + rect), "window" (window_id + rect),
          "pick-window" / "pick-monitor" (let the system dialog choose, portal only).
    """

    kind: str
    rect: Rect | None = None
    connector: str | None = None
    window_id: int = 0


@dataclass
class WindowInfo:
    id: int
    title: str
    app: str
    rect: Rect


@dataclass
class Stream:
    """A running PipeWire video stream.

    source_rect: the desktop area (logical px) the stream covers, when known.
    crop: part of the stream to keep, relative to source_rect, in logical px (portal region).
    """

    node_id: int
    fd: int | None
    source_rect: Rect | None
    crop: Rect | None
    close: callable


class Backend:
    name = "base"
    # Can we ask where the pointer is? (needed to highlight clicks)
    has_pointer = False
    # Do we list windows ourselves, or does the system pick them?
    lists_windows = False
    # Can we show our own "stop recording" indicator?
    has_indicator = False

    def available(self):
        return True

    def freeze(self, cursor, callback):
        """Capture the whole desktop. callback(cairo.ImageSurface | None, error | None)."""
        raise NotImplementedError

    def list_windows(self, exclude_pid):
        return []

    def open_stream(self, target, cursor, recording, callback, on_closed=None):
        """Start a PipeWire stream for target. callback(Stream | None, error | None)."""
        raise NotImplementedError

    def pointer(self, window_id, callback):
        """callback(x, y, window_rect | None) in logical desktop px."""
        callback(None, None, None)

    def set_indicator(self, active):
        pass

    def connect_stop_requested(self, callback):
        pass
