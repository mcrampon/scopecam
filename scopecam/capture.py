"""Pull a single frame out of a PipeWire stream (used for screenshots), and find the
window inside window-stream frames."""

import ctypes

import cairo
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

Gst.init(None)

# Client-side shadows are at most this wide (px) on each side of a window.
MAX_SHADOW = 96


def pipewire_source(stream):
    # keepalive/resend-last: Mutter only sends frames on damage; these keep a static
    # screen flowing at a steady rate (same settings GNOME's own recorder uses).
    src = f"pipewiresrc path={stream.node_id} do-timestamp=true keepalive-time=1000 resend-last=true"
    if stream.fd is not None:
        src += f" fd={stream.fd}"
    return src


# --- window streams -------------------------------------------------------------------
#
# Mutter sends window streams as frames the size of a monitor, with the window in a
# corner and a GstVideoCropMeta saying where. Nothing in our pipeline applies that meta,
# and PyGObject cannot read its fields, so read it through ctypes.


class _Native:
    loaded = False
    gst = video = None
    api = 0

    @classmethod
    def load(cls):
        if cls.loaded:
            return cls.gst is not None
        cls.loaded = True
        try:
            # Already loaded by GObject introspection, so the soname resolves.
            gst = ctypes.CDLL("libgstreamer-1.0.so.0")
            video = ctypes.CDLL("libgstvideo-1.0.so.0")
            video.gst_video_crop_meta_api_get_type.restype = ctypes.c_size_t
            gst.gst_buffer_get_meta.restype = ctypes.c_void_p
            gst.gst_buffer_get_meta.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            gst.gst_buffer_get_size.restype = ctypes.c_size_t
            gst.gst_buffer_get_size.argtypes = [ctypes.c_void_p]
            cls.api = video.gst_video_crop_meta_api_get_type()
            cls.gst, cls.video = gst, video
        except (OSError, AttributeError):
            return False
        return True


def crop_meta(buf):
    """(x, y, w, h) from the buffer's GstVideoCropMeta, or None."""
    if not _Native.load():
        return None
    # A PyGObject boxed wrapper stores the C pointer right after the PyObject header.
    ptr = ctypes.c_void_p.from_address(id(buf) + object.__basicsize__).value
    if not ptr or _Native.gst.gst_buffer_get_size(ptr) != buf.get_size():
        return None  # layout not what we expect: don't touch it
    meta = _Native.gst.gst_buffer_get_meta(ptr, _Native.api)
    if not meta:
        return None
    # struct GstVideoCropMeta { GstMeta meta (16 bytes); guint x, y, width, height; }
    x, y, w, h = (ctypes.c_uint * 4).from_address(meta + 16)
    return (x, y, w, h) if w and h else None


def window_region(buf, info):
    """The part of a BGRA window-stream frame that holds the window, as (x, y, w, h).

    Starts from the crop meta (window plus its shadow), then trims client-side shadows by
    keeping the fully opaque part, which is the window frame. Translucent windows keep
    the crop meta's region.
    """
    region = crop_meta(buf) or (0, 0, info.width, info.height)
    rx, ry, rw, rh = region
    data = buf.extract_dup(0, buf.get_size())
    stride = info.stride[0]

    def alpha_row(y):
        start = y * stride + rx * 4 + 3
        return data[start:start + rw * 4:4]

    rows = [y for y in range(ry, ry + rh) if b"\xff" in alpha_row(y)]
    if not rows:
        return region
    top, bottom = rows[0], rows[-1]
    left, right = rw, -1
    for y in rows[:: max(1, len(rows) // 32)] + [top, bottom]:
        a = alpha_row(y)
        left = min(left, a.find(b"\xff"))
        right = max(right, a.rfind(b"\xff"))
    opaque = (rx + left, top, right - left + 1, bottom - top + 1)
    # Only trust it if what we cut away looks like a shadow, not window content.
    if opaque[2] >= rw - 2 * MAX_SHADOW and opaque[3] >= rh - 2 * MAX_SHADOW:
        return opaque
    return region


# --- frames -------------------------------------------------------------------------


def surface_from_sample(sample, window=False):
    caps = sample.get_caps()
    info = GstVideo.VideoInfo.new_from_caps(caps)
    buf = sample.get_buffer()
    data = bytearray(buf.extract_dup(0, buf.get_size()))
    stride = info.stride[0]
    # BGRx/BGRA are cairo's RGB24/ARGB32 layouts on little-endian machines. Mutter's
    # buffers are premultiplied, as cairo expects.
    fmt = cairo.FORMAT_ARGB32 if window else cairo.FORMAT_RGB24
    surface = cairo.ImageSurface.create_for_data(data, fmt, info.width, info.height, stride)
    x, y, w, h = window_region(buf, info) if window else (0, 0, info.width, info.height)
    # Copy (and crop) so the surface owns its memory.
    return crop_surface(surface, x, y, w, h, alpha=window)


def grab_frame(stream, callback, timeout_ms=5000, window=False):
    """callback(cairo.ImageSurface | None, error | None), called once on the main loop.

    window: the stream is a window; crop to it and keep transparency (rounded corners).
    """
    fmt = "BGRA" if window else "BGRx"
    pipeline = Gst.parse_launch(
        f"{pipewire_source(stream)} ! videoconvert ! video/x-raw,format={fmt} "
        "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    sink = pipeline.get_by_name("sink")
    state = {"done": False, "timeout": 0}

    def finish(surface, error):
        if state["done"]:
            return False
        state["done"] = True
        if state["timeout"]:
            GLib.source_remove(state["timeout"])
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        callback(surface, error)
        return False

    def on_sample(appsink):
        sample = appsink.emit("pull-sample")
        try:
            surface = surface_from_sample(sample, window)
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(finish, None, f"Could not read frame: {e}")
        else:
            GLib.idle_add(finish, surface, None)
        return Gst.FlowReturn.EOS

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            finish(None, f"Capture failed: {err.message}")

    sink.connect("new-sample", on_sample)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    state["timeout"] = GLib.timeout_add(timeout_ms, lambda: finish(None, "Timed out waiting for a frame"))
    pipeline.set_state(Gst.State.PLAYING)


def crop_surface(surface, x, y, w, h, alpha=False):
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32 if alpha else cairo.FORMAT_RGB24, max(1, w), max(1, h))
    cr = cairo.Context(out)
    cr.set_source_surface(surface, -x, -y)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.paint()
    out.flush()
    return out
