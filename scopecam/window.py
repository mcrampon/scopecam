"""Small window shown when Scopecam is opened while a recording is running."""

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


class RecordingWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Scopecam", resizable=False, default_width=320)
        self.app = app
        page = Adw.StatusPage(icon_name="media-record-symbolic", title="Recording")
        page.add_css_class("compact")
        stop = Gtk.Button(label="Stop Recording", halign=Gtk.Align.CENTER)
        stop.add_css_class("pill")
        stop.add_css_class("destructive-action")
        stop.connect("clicked", lambda *_: (app.stop_recording(), self.close()))
        page.set_child(stop)
        self.page = page
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        self.set_content(view)
        self._timer = 0
        self._tick()
        self._timer = GLib.timeout_add(500, self._tick)
        self.connect("close-request", self._on_close)

    def _tick(self):
        rec = self.app.recording
        if not rec:
            self._timer = 0
            self.close()
            return False
        secs = int(time.monotonic() - rec.started)
        self.page.set_description(f"{secs // 60:02d}:{secs % 60:02d}")
        return True

    def _on_close(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        return False
