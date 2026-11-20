"""Pull a single frame out of a PipeWire stream (used for screenshots)."""

import cairo
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

Gst.init(None)


def pipewire_source(stream):
    # keepalive/resend-last: Mutter only sends frames on damage; these keep a static
    # screen flowing at a steady rate (same settings GNOME's own recorder uses).
    src = f"pipewiresrc path={stream.node_id} do-timestamp=true keepalive-time=1000 resend-last=true"
    if stream.fd is not None:
        src += f" fd={stream.fd}"
    return src


def surface_from_sample(sample):
    caps = sample.get_caps()
    info = GstVideo.VideoInfo.new_from_caps(caps)
    buf = sample.get_buffer()
    data = bytearray(buf.extract_dup(0, buf.get_size()))
    stride = info.stride[0]
    # BGRx is cairo's RGB24 layout on little-endian machines.
    surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, info.width, info.height, stride)
    # Copy so the surface owns its memory.
    out = cairo.ImageSurface(cairo.FORMAT_RGB24, info.width, info.height)
    cr = cairo.Context(out)
    cr.set_source_surface(surface, 0, 0)
    cr.paint()
    out.flush()
    return out


def grab_frame(stream, callback, timeout_ms=5000):
    """callback(cairo.ImageSurface | None, error | None), called once on the main loop."""
    pipeline = Gst.parse_launch(
        f"{pipewire_source(stream)} ! videoconvert ! video/x-raw,format=BGRx "
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
            surface = surface_from_sample(sample)
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


def crop_surface(surface, x, y, w, h):
    out = cairo.ImageSurface(cairo.FORMAT_RGB24, max(1, w), max(1, h))
    cr = cairo.Context(out)
    cr.set_source_surface(surface, -x, -y)
    cr.paint()
    out.flush()
    return out
