"""Screencast recording: PipeWire video (+ optional audio and overlay) into a file."""

import os
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from gi.repository import GstVideo  # noqa: E402

from .capture import crop_meta, pipewire_source, window_region  # noqa: E402

# SVT-AV1 prints a banner and settings dump on every start; keep only errors.
os.environ.setdefault("SVT_LOG", "1")
Gst.init(None)


@dataclass(frozen=True)
class Encoding:
    label: str
    hint: str
    container: str  # "mp4" | "webm"
    elements: tuple  # encoder elements that must exist (any one of them for alternatives)
    raw_format: str  # pixel format the encoder takes

    @property
    def ext(self):
        return self.container

    def available(self):
        return any(Gst.ElementFactory.find(e) for e in self.elements)


# All tuned for real-time capture: they must keep up with the screen on a normal CPU.
ENCODINGS = {
    "h264": Encoding("H.264 · MP4", "Plays everywhere", "mp4", ("x264enc", "openh264enc"), "I420"),
    "h264-hw": Encoding("H.264 (hardware) · MP4", "Plays everywhere; encoded on the GPU, light on the CPU",
                        "mp4", ("vah264enc", "vah264lpenc"), "NV12"),
    "h265-hw": Encoding("H.265 (hardware) · MP4", "Smaller files; not every browser or app can play it",
                        "mp4", ("vah265enc", "vah265lpenc"), "NV12"),
    "vp8": Encoding("VP8 · WebM", "Open format, fast; larger files", "webm", ("vp8enc",), "I420"),
    "vp9": Encoding("VP9 · WebM", "Open format, smaller files; heavier on the CPU", "webm", ("vp9enc",), "I420"),
    "av1": Encoding("AV1 · WebM", "Smallest files; heavy on the CPU, needs a recent player",
                    "webm", ("svtav1enc",), "I420"),
}
DEFAULT_ENCODING = "h264"


def available_encodings():
    return {k: e for k, e in ENCODINGS.items() if e.available()}


def _has(*names):
    return all(Gst.ElementFactory.find(n) for n in names)


def _first(*names):
    return next(n for n in names if Gst.ElementFactory.find(n))


def _video_encoder(key, fps):
    threads = max(1, min(os.cpu_count() or 1, 16))
    gop = 2 * fps
    if key == "h264-hw":
        return (f"{_first('vah264enc', 'vah264lpenc')} rate-control=cqp qpi=22 qpp=24 qpb=26 key-int-max={gop} "
                "! h264parse")
    if key == "h265-hw":
        return (f"{_first('vah265enc', 'vah265lpenc')} rate-control=cqp qpi=24 qpp=26 qpb=28 key-int-max={gop} "
                "! h265parse")
    if key == "vp8":
        return (f"vp8enc cpu-used=16 max-quantizer=17 deadline=1 keyframe-mode=disabled "
                f"threads={threads} static-threshold=1000 buffer-size=20000")
    if key == "vp9":
        return (f"vp9enc deadline=1 cpu-used=8 row-mt=true tile-columns=2 threads={threads} "
                f"end-usage=cq cq-level=30 min-quantizer=0 max-quantizer=50 keyframe-max-dist={gop}")
    if key == "av1":
        # Preset 9: fastest one SVT-AV1 still considers artifact-free outside its RTC mode.
        return f"svtav1enc preset=9 crf=35 intra-period-length={gop} ! av1parse"
    if _has("x264enc"):
        return (f"x264enc speed-preset=superfast pass=qual quantizer=20 key-int-max={gop} "
                "threads=0 ! video/x-h264,profile=high ! h264parse")
    return "openh264enc complexity=low ! h264parse"


def _audio_encoder(container):
    if container == "webm":
        return "opusenc bitrate=128000"
    for enc in ("fdkaacenc", "avenc_aac", "voaacenc"):
        if _has(enc):
            return f"{enc} bitrate=192000 ! aacparse" if enc == "avenc_aac" else f"{enc} ! aacparse"
    return None


def _muxer(container):
    if container == "webm":
        return "webmmux name=mux"
    # Fragmented while recording (survives crashes), rewritten as a regular MP4 at the end.
    return "mp4mux name=mux fragment-duration=1000 fragment-mode=first-moov-then-finalise"


def _audio_source(device, name):
    dev = f" device={device}" if device else ""
    return (f"pulsesrc name={name}{dev} provide-clock=false do-timestamp=true ! audioconvert ! audioresample "
            "! audio/x-raw,rate=48000,channels=2 ! queue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0")


class Recorder:
    """Records `stream` to `path`. on_finished(path | None, error | None) runs on the main loop."""

    def __init__(self, stream, path, encoding=DEFAULT_ENCODING, fps=30, desktop_audio=False, microphone=False,
                 overlay=None, on_finished=None, window=False):
        """window: the stream is a window, sent by Mutter inside a monitor-sized frame;
        crop to the window (following it if it is resized)."""
        self.stream = stream
        self.window = window
        self.path = path
        self.overlay = overlay
        self.on_finished = on_finished
        if encoding not in ENCODINGS or not ENCODINGS[encoding].available():
            encoding = DEFAULT_ENCODING
        enc = ENCODINGS[encoding]
        self.out_size = None  # (w, h) of the recorded video
        self.crop_px = (0, 0)  # (left, top) cropped from the stream, in stream px
        self.scale = 1.0  # stream px per logical px
        self._stopping = False
        self._done = False
        self._sized = False
        self._window_meta = False  # last crop meta seen (False: none yet)

        # With an overlay, keep frames flowing at the target rate so animations are smooth
        # on a static screen; otherwise once a second is enough.
        src = pipewire_source(stream)
        if overlay:
            src = src.replace("keepalive-time=1000", f"keepalive-time={max(10, 1000 // fps)}")
        video = (
            f"{src} name=vsrc ! videoconvert chroma-mode=none dither=none "
            f"! video/x-raw,format={'BGRA' if window else 'BGRx'} "
            "! videocrop name=crop ! videoscale add-borders=true ! capsfilter name=size "
            f"! videorate ! video/x-raw,framerate={fps}/1 "
            + ("! cairooverlay name=overlay " if overlay else "")
            + f"! videoconvert ! video/x-raw,format={enc.raw_format} ! queue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0 "
            f"! {_video_encoder(encoding, fps)} ! queue ! mux."
        )
        parts = [video, f"{_muxer(enc.container)} ! filesink name=sink location={_quote(path)}"]

        sources = []
        if desktop_audio:
            sources.append(_audio_source("@DEFAULT_MONITOR@", f"asrc{len(sources)}"))
        if microphone:
            sources.append(_audio_source(None, f"asrc{len(sources)}"))
        aenc = _audio_encoder(enc.container) if sources else None
        if not aenc:
            sources = []
        self._audio_names = [f"asrc{i}" for i in range(len(sources))]
        if sources:
            if len(sources) == 1:
                parts.append(f"{sources[0]} ! audioconvert ! audio/x-raw,channels=2 ! {aenc} ! queue ! mux.")
            else:
                parts.append(f"audiomixer name=amix ! audioconvert ! audio/x-raw,channels=2 ! {aenc} ! queue ! mux.")
                parts += [f"{s} ! amix." for s in sources]

        self.pipeline = Gst.parse_launch(" ".join(parts))
        crop_pad = self.pipeline.get_by_name("crop").get_static_pad("sink")
        if window:
            crop_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_window_buffer)
        else:
            crop_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_event)
        if overlay:
            ov = self.pipeline.get_by_name("overlay")
            ov.connect("draw", overlay.draw)
            ov.connect("caps-changed", overlay.on_caps)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    # --- geometry ---------------------------------------------------------------

    def _on_event(self, _pad, info):
        event = info.get_event()
        if event.type != Gst.EventType.CAPS or self._sized:
            return Gst.PadProbeReturn.OK
        s = event.parse_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        left = top = right = bottom = 0
        src_rect, crop = self.stream.source_rect, self.stream.crop
        if src_rect and src_rect.w:
            self.scale = w / src_rect.w
        if crop:
            sc = self.scale
            left, top = round(crop.x * sc), round(crop.y * sc)
            right = max(0, w - left - round(crop.w * sc))
            bottom = max(0, h - top - round(crop.h * sc))
        # H.264 wants even dimensions.
        right += (w - left - right) % 2
        bottom += (h - top - bottom) % 2
        out_w, out_h = w - left - right, h - top - bottom
        crop_el = self.pipeline.get_by_name("crop")
        crop_el.set_property("left", left)
        crop_el.set_property("top", top)
        crop_el.set_property("right", right)
        crop_el.set_property("bottom", bottom)
        # Fix the output size: if a recorded window is resized, it gets scaled to fit.
        self.pipeline.get_by_name("size").set_property(
            "caps", Gst.Caps.from_string(f"video/x-raw,width={out_w},height={out_h}"))
        self.crop_px = (left, top)
        self.out_size = (out_w, out_h)
        self._sized = True
        return Gst.PadProbeReturn.OK

    def _on_window_buffer(self, pad, info):
        buf = info.get_buffer()
        meta = crop_meta(buf)
        if meta == self._window_meta:
            return Gst.PadProbeReturn.OK
        # First frame, or the window was resized: find it in the frame again.
        self._window_meta = meta
        vinfo = GstVideo.VideoInfo.new_from_caps(pad.get_current_caps())
        x, y, w, h = window_region(buf, vinfo)
        if not self._sized:
            # H.264 wants even dimensions.
            w -= w % 2
            h -= h % 2
            self.pipeline.get_by_name("size").set_property(
                "caps", Gst.Caps.from_string(f"video/x-raw,width={w},height={h}"))
            self.out_size = (w, h)
            self._sized = True
        crop = self.pipeline.get_by_name("crop")
        crop.set_property("left", x)
        crop.set_property("top", y)
        crop.set_property("right", max(0, vinfo.width - x - w))
        crop.set_property("bottom", max(0, vinfo.height - y - h))
        # The new crop applies from the next frame; don't let this one through uncropped.
        return Gst.PadProbeReturn.DROP

    def to_video(self, x, y, source_rect=None):
        """Map a logical desktop point to video pixels (None if unknown)."""
        rect = source_rect or self.stream.source_rect
        if not rect or not self.out_size:
            return None
        if self.window:
            # The video is the window frame, scaled to the output size.
            return ((x - rect.x) * self.out_size[0] / rect.w, (y - rect.y) * self.out_size[1] / rect.h)
        return ((x - rect.x) * self.scale - self.crop_px[0], (y - rect.y) * self.scale - self.crop_px[1])

    def running_time(self):
        clock = self.pipeline.get_clock()
        if not clock:
            return 0
        return clock.get_time() - self.pipeline.get_base_time()

    # --- lifecycle ----------------------------------------------------------------

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._finish("Could not start recording pipeline")

    def stop(self):
        """Finish the file cleanly (EOS), then call on_finished.

        Calling it again while a stop is pending finishes right away, so the user can
        always get out even if the pipeline never drains.
        """
        if self._done:
            return
        if self._stopping:
            self._force_stop()
            return
        self._stopping = True
        GLib.timeout_add_seconds(5, self._force_stop)
        self.pipeline.send_event(Gst.Event.new_eos())

    def source_lost(self):
        """The video stream went away (e.g. stopped from GNOME's sharing indicator): finalise."""
        if self._stopping or self._done:
            return
        self._stopping = True
        GLib.timeout_add_seconds(5, self._force_stop)
        # The video source can no longer push EOS itself: inject it right after it, and end
        # the audio sources so the muxer can write a complete file.
        self.pipeline.get_by_name("crop").get_static_pad("sink").send_event(Gst.Event.new_eos())
        for name in self._audio_names:
            self.pipeline.get_by_name(name).send_event(Gst.Event.new_eos())

    def _force_stop(self):
        if not self._done:
            self._finish("Recording did not finish cleanly; the file may be incomplete")
        return False

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._finish(None)
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            src = msg.src.get_name() if msg.src else ""
            if src == "vsrc" or (msg.src and msg.src.get_parent() and "vsrc" in msg.src.get_path_string()):
                # Screen stream ended: keep what we have.
                print(f"scopecam: video source error ({err.message}); finalising")
                self.source_lost()
                return
            print(f"scopecam: pipeline error from {src}: {err.message}\n{dbg}")
            if self._stopping:
                self._finish(None if os.path.getsize(self.path) > 0 else err.message)
            else:
                self._finish(err.message)

    def _finish(self, error):
        if self._done:
            return
        self._done = True
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()
        self.stream.close()
        ok = error is None and os.path.exists(self.path) and os.path.getsize(self.path) > 0
        if self.on_finished:
            self.on_finished(self.path if ok else None, error)


def _quote(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
