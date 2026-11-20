"""Application: CLI entry point, capture orchestration and recording lifecycle."""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, VERSION  # noqa: E402
from .backends import detect  # noqa: E402
from .backends.base import CURSOR_EMBEDDED, CURSOR_HIDDEN, Target  # noqa: E402
from .capture import crop_surface, grab_frame  # noqa: E402
from .config import Config  # noqa: E402
from .geometry import Rect, desktop_rect, monitors, primary_connector  # noqa: E402
from .recorder import DEFAULT_ENCODING, ENCODINGS, Recorder  # noqa: E402
from .selector import Choice  # noqa: E402

MODES = ("region", "screen", "window")


class Recording:
    def __init__(self, recorder, target, overlay, watcher):
        self.recorder = recorder
        self.target = target
        self.overlay = overlay
        self.watcher = watcher
        self.started = time.monotonic()


class ScopecamApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.config = Config.load()
        self.backend = None
        self.window = None  # RecordingWindow, while one is open
        self.busy = False
        self._delay = None
        self.recording = None
        self._clipboard_held = False
        self._token = None

    # --- setup ----------------------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.backend = detect(self.bus, self.config)
        self.backend.connect_stop_requested(self.stop_recording)

        def action(name, cb, param=None):
            act = Gio.SimpleAction.new(name, GLib.VariantType(param) if param else None)
            act.connect("activate", lambda _a, p: cb(p.unpack() if p is not None else None))
            self.add_action(act)

        action("stop", lambda _: self.stop_recording())
        action("open-file", self._open_file, "s")
        action("show-file", self._show_file, "s")
        action("open-folder", self._open_folder, "s")
        action("preferences", lambda _: self._preferences())
        action("about", lambda _: self._about())
        action("quit", lambda _: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])

    def do_command_line(self, command_line):
        parser = _parser()
        try:
            args = parser.parse_args(command_line.get_arguments()[1:])
        except SystemExit:
            return 1
        cmd = args.command
        if cmd in (None, "window"):
            # Always open ready for a screenshot, with the last-used mode.
            self.open_ui("screenshot", self.config.mode)
        elif cmd == "preferences":
            self._preferences()
        elif cmd == "stop":
            self.stop_recording()
        elif cmd == "toggle" and self.recording:
            self.stop_recording()
        elif cmd in ("screenshot", "record", "toggle"):
            kind = "screenshot" if cmd == "screenshot" else "screencast"
            self.open_ui(kind, args.mode or self.config.mode, delay=args.delay, explicit=args.mode is not None)
        return 0

    # --- capture flow ---------------------------------------------------------------

    def open_ui(self, kind, mode, delay=None, explicit=False):
        """Freeze the screen and show the capture overlay.

        explicit: the mode came from the command line; skip the overlay when there is
        nothing to choose (one screen, or windows picked by the system dialog).
        """
        if self.recording:
            return self._show_recording_window()
        if self.busy:
            return
        self.busy = True
        self.hold()
        self._delay = delay
        mons = monitors()
        if explicit and mode == "window" and not self.backend.lists_windows:
            return self._act(Choice(kind, Target("pick-window"), False), None)
        if explicit and mode == "screen" and len(mons) == 1:
            m = mons[0]
            return self._act(Choice(kind, Target("monitor", m.rect, m.connector), False), None)

        def frozen(frames, error):
            if error:
                return self._fail(error)
            windows = self.backend.list_windows(os.getpid())
            initial = Rect(*self.config.last_region) if len(self.config.last_region) == 4 else None
            if initial and not desktop_rect(mons).intersect(initial):
                initial = None
            from .selector import Selector

            def show(x=None, y=None, _rect=None):
                focus = (x, y) if x is not None else primary_connector(self.bus)
                token, self._token = self._token, None
                holder["selector"] = Selector(
                    self, frames, desktop_rect(mons), mons, kind, mode,
                    lambda choice: self._act(choice, holder["selector"], frames),
                    windows=windows, can_list_windows=self.backend.lists_windows,
                    initial=initial, focus=focus, startup_id=token)

            holder = {}
            if self.backend.has_pointer:
                self.backend.pointer(0, show)
            else:
                show()

        self._freeze_all(frozen)

    def _freeze_all(self, callback):
        """Freeze the desktop without the pointer and, when the backend can, with it, so the
        pointer option can be toggled in the overlay. callback({"plain", "cursor"}, error)."""
        frames = {}
        pending = {"n": 2 if self.backend.name == "gnome" else 1, "error": None}

        def got(key, surface, error):
            frames[key] = surface
            pending["error"] = pending["error"] or error
            pending["n"] -= 1
            if pending["n"] == 0:
                if frames.get("plain") is None:
                    return callback(None, pending["error"] or "Could not capture the screen")
                callback(frames, None)

        if pending["n"] == 2:
            self.backend.freeze(CURSOR_HIDDEN, lambda s, e: got("plain", s, e))
            self.backend.freeze(CURSOR_EMBEDDED, lambda s, e: got("cursor", s, e))
        else:
            self.backend.freeze(CURSOR_HIDDEN, lambda s, e: got("plain", s, e))

    def _act(self, choice, selector, frames=None):
        """Carry out what was picked in the overlay (or on the command line)."""
        if choice is None:
            return self._done()
        target = choice.target
        if target.kind == "area":
            self.config.last_region = target.rect.as_list()
            self.config.save()
        delay = self.config.delay if self._delay is None else self._delay

        if choice.kind == "screencast":
            if selector:
                selector.close()
            # Let the compositor unmap the overlay before recording starts.
            GLib.timeout_add(150 + delay * 1000, lambda: self._start_recording(target) and False)
            return

        if delay or frames is None or target.kind in ("window", "pick-window"):
            # Capture live: after the delay, or a window (which Mutter renders on its own,
            # so the overlay on top does not matter and can stay up for the clipboard).
            if selector and (delay or target.kind == "pick-window"):
                selector.close()
                selector = None
            if self._token is None and selector is None:
                self._token = self._activation_token()
            GLib.timeout_add(150 + delay * 1000 if (delay or frames is None) else 1,
                             lambda: self._capture_live(target, choice.copy, selector) and False)
            return

        frame = frames["cursor"] if self.config.screenshot_pointer and frames.get("cursor") else frames["plain"]
        self._save_screenshot(_crop(frame, target.rect), target, choice.copy, selector)

    def _capture_live(self, target, copy, selector):
        cursor = CURSOR_EMBEDDED if self.config.screenshot_pointer else CURSOR_HIDDEN

        def fail(message):
            if selector:
                selector.close()
            self._fail(message)

        if target.kind in ("area", "monitor"):
            def frozen(frame, error):
                if error:
                    return fail(error)
                self._save_screenshot(_crop(frame, target.rect), target, copy, selector)

            return self.backend.freeze(cursor, frozen)

        def on_stream(stream, error):
            if error:
                return fail(error)

            def grabbed(surface, err):
                stream.close()
                if err:
                    return fail(err)
                self._save_screenshot(surface, target, copy, selector)

            grab_frame(stream, grabbed, window=target.kind in ("window", "pick-window"))

        self.backend.open_stream(target, cursor, False, on_stream)

    def _save_screenshot(self, surface, target, copy, selector):
        path = self._new_path("screenshot", "png")
        try:
            surface.write_to_png(path)
        except OSError as e:
            if selector:
                selector.close()
            return self._fail(f"Could not save screenshot: {e}")

        def copied(ok):
            note = None if ok else "Not copied to the clipboard: Scopecam could not get focus"
            self._announce("Screenshot captured", path, note)
            self._done()

        self._copy_with_focus(path, target, selector, copy or self.config.copy_to_clipboard, copied)

    # --- recording ------------------------------------------------------------------

    def _start_recording(self, target):
        cfg = self.config
        cursor = CURSOR_EMBEDDED if cfg.screencast_pointer else CURSOR_HIDDEN
        holder = {}

        def on_closed():
            # The capture was stopped from outside (e.g. the system's sharing indicator).
            if self.recording and self.recording is holder.get("rec"):
                self.recording.recorder.source_lost()

        def on_stream(stream, error):
            if error:
                return self._fail(error)
            encoding = cfg.encoding if cfg.encoding in ENCODINGS and ENCODINGS[cfg.encoding].available() \
                else DEFAULT_ENCODING
            path = self._new_path("screencast", ENCODINGS[encoding].ext)
            want_clicks = cfg.screencast_pointer and cfg.show_clicks and self.backend.has_pointer
            overlay = watcher = None
            if cfg.show_keys or want_clicks:
                from .overlay import Overlay

                overlay = Overlay(show_keys=cfg.show_keys, show_clicks=want_clicks,
                                  shortcuts_only=cfg.keys_mode == "shortcuts")
            try:
                recorder = Recorder(stream, path, encoding=encoding, fps=cfg.framerate,
                                    desktop_audio=cfg.record_desktop_audio, microphone=cfg.record_microphone,
                                    overlay=overlay, on_finished=self._recording_finished,
                                    window=target.kind in ("window", "pick-window"))
            except GLib.Error as e:
                stream.close()
                return self._fail(f"Could not set up recording: {e.message}")
            if overlay:
                from .inputwatch import InputWatcher

                watcher = InputWatcher(
                    on_key=(lambda code, name, pressed: overlay.key(code, name, pressed, recorder.running_time()))
                    if cfg.show_keys else None,
                    on_button=self._on_button if want_clicks else None,
                    on_error=lambda msg: self._notify_error(msg),
                )
            rec = holder["rec"] = Recording(recorder, target, overlay, watcher)
            self.recording = rec
            self.busy = False
            recorder.start()
            if watcher:
                watcher.start()
            self.backend.set_indicator(True)
            if not self.backend.has_indicator or not getattr(self.backend, "has_extension", True):
                self._notify_recording()

        self.backend.open_stream(target, cursor, True, on_stream, on_closed)

    def _on_button(self, name, pressed):
        rec = self.recording
        if not rec or not pressed:
            return
        t = rec.recorder.running_time()
        window_id = rec.target.window_id if rec.target.kind == "window" else 0

        def at(x, y, window_rect):
            if x is None or self.recording is not rec:
                return
            point = rec.recorder.to_video(x, y, window_rect)
            if point:
                rec.overlay.click(point[0], point[1], name, t)

        self.backend.pointer(window_id, at)

    def stop_recording(self):
        if self.recording:
            self.recording.recorder.stop()

    def _recording_finished(self, path, error):
        rec, self.recording = self.recording, None
        if rec and rec.watcher:
            rec.watcher.stop()
        self.backend.set_indicator(False)
        self.withdraw_notification("recording")
        if path:
            self._announce("Screencast recorded", path)
            if error:
                self._notify_error(error)
            self._done()
        else:
            self._fail(error or "Recording failed")

    # --- helpers --------------------------------------------------------------------

    def _new_path(self, kind, ext):
        cfg = self.config
        folder = Path(os.path.expanduser(cfg.screenshot_dir if kind == "screenshot" else cfg.screencast_dir))
        folder.mkdir(parents=True, exist_ok=True)
        pattern = cfg.screenshot_name if kind == "screenshot" else cfg.screencast_name
        try:
            stem = datetime.now().strftime(pattern).replace("/", "-")
        except ValueError:
            stem = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        path = folder / f"{stem}.{ext}"
        n = 2
        while path.exists():
            path = folder / f"{stem} ({n}).{ext}"
            n += 1
        return str(path)

    def _activation_token(self):
        try:
            ctx = Gdk.Display.get_default().get_app_launch_context()
            return ctx.get_startup_notify_id(None, []) or None
        except (TypeError, GLib.Error):
            return None

    def _copy_with_focus(self, path, target, selector, want_copy, done):
        """Copy the image to the clipboard, which Wayland only allows from a focused window.

        Uses the selection overlay if it still has focus; otherwise flashes the screen
        (like GNOME does), which takes focus for a moment. done(copied) runs afterwards.
        """
        if not want_copy:
            if selector:
                selector.close()
            return done(True)
        if selector and selector.has_focus():
            self._copy_image(path)
            selector.close()
            return done(True)
        if selector:
            selector.close()
        from .flash import Flash

        mons = monitors()
        rect = target.rect if target else None
        mon = None
        if rect:
            cx, cy = rect.x + rect.w / 2, rect.y + rect.h / 2
            mon = next((m for m in mons if m.rect.contains(cx, cy)), None)
        if mon is None:
            primary = primary_connector(self.bus)
            mon = next((m for m in mons if m.connector == primary), mons[0])
        token, self._token = self._token, None

        def focused(ok):
            if ok:
                self._copy_image(path)
            done(ok)

        Flash(self, mon, focused, startup_id=token)

    def _copy_image(self, path):
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            print("scopecam: clipboard:", e)
            return
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set_content(Gdk.ContentProvider.new_for_bytes("image/png", GLib.Bytes.new(data)))
        # Wayland clipboards live in the owning app: stay alive until something else is copied.
        if not self._clipboard_held:
            self._clipboard_held = True
            self.hold()

            def changed(cb):
                if not cb.is_local() and self._clipboard_held:
                    self._clipboard_held = False
                    cb.disconnect(handler)
                    self.release()

            handler = clipboard.connect("changed", changed)

    def _announce(self, title, path, note=None):
        if note:
            print("scopecam:", note, file=sys.stderr)
        n = Gio.Notification.new(title)
        n.set_body(os.path.basename(path) + (f"\n{note}" if note else ""))
        n.set_icon(Gio.ThemedIcon.new("applets-screenshooter-symbolic"))
        n.set_default_action_and_target("app.open-file", GLib.Variant("s", path))
        n.add_button_with_target("Show in Files", "app.show-file", GLib.Variant("s", path))
        self.send_notification(None, n)

    def _notify_recording(self):
        n = Gio.Notification.new("Recording screen")
        n.set_body("Use “Stop” here, or run `scopecam stop`.")
        n.set_priority(Gio.NotificationPriority.HIGH)
        n.add_button("Stop", "app.stop")
        n.set_default_action("app.stop")
        self.send_notification("recording", n)

    def _notify_error(self, message):
        print("scopecam:", message, file=sys.stderr)
        n = Gio.Notification.new("Scopecam")
        n.set_body(message)
        self.send_notification(None, n)

    def _fail(self, message):
        self._notify_error(message)
        self._done()

    def _done(self):
        self.busy = False
        self._delay = None
        self.release()

    def _show_recording_window(self):
        if not self.window:
            from .window import RecordingWindow

            self.window = RecordingWindow(self)
            self.window.connect("close-request", lambda *_: setattr(self, "window", None) or False)
        self.window.present()

    def _open_file(self, path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.window, None, None)

    def _show_file(self, path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(self.window, None, None)

    def _open_folder(self, kind):
        folder = self.config.screenshot_dir if kind == "screenshot" else self.config.screencast_dir
        Path(folder).mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(folder)).launch(self.window, None, None)

    def _preferences(self):
        from .prefs import PreferencesDialog

        # Over the capture overlay when it is open, else on its own.
        parent = next((w for w in self.get_windows() if w.is_active()), None)
        if parent is None:
            self.hold()
            dialog = PreferencesDialog(self)
            dialog.connect("closed", lambda *_: self.release())
            dialog.present(None)
            return
        PreferencesDialog(self).present(parent)

    def _about(self):
        Adw.AboutDialog(application_name="Scopecam", application_icon="applets-screenshooter-symbolic",
                        version=VERSION, comments="Screenshots and screencasts, with typed keys and clicks.",
                        license_type=Gtk.License.GPL_3_0).present(
            next((w for w in self.get_windows() if w.is_active()), None))


def _parser():
    p = argparse.ArgumentParser(prog="scopecam", description="Take screenshots and record screencasts.")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("window", help="open the capture overlay, ready for a screenshot (default)")
    sub.add_parser("preferences", help="open the preferences")
    for name, help_ in (("screenshot", "take a screenshot"), ("record", "start recording"),
                        ("toggle", "start recording, or stop the current one")):
        sp = sub.add_parser(name, help=help_)
        g = sp.add_mutually_exclusive_group()
        for mode in MODES:
            g.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode, help=f"capture a {mode}")
        sp.add_argument("--delay", type=int, default=None, metavar="SECONDS")
    sub.add_parser("stop", help="stop the current recording")
    return p


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        _parser().print_help()
        return 0
    return ScopecamApp().run(sys.argv)


def _crop(frame, rect):
    """Cut a desktop rect (logical px) out of a full-desktop frame."""
    desk = desktop_rect()
    s = frame.get_width() / desk.w
    return crop_surface(frame, round((rect.x - desk.x) * s), round((rect.y - desk.y) * s),
                        round(rect.w * s), round(rect.h * s))
