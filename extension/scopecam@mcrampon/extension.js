/* Scopecam helper: exposes what Mutter does not give to regular apps.
 *
 * D-Bus service io.github.mcrampon.Scopecam.Shell on the session bus:
 *   GetWindows(u exclude_pid) -> s json   windows on the active workspace, topmost first
 *   GetPointer(t window_id) -> (i x, i y, u mods, (iiii) window_frame_rect)
 *   SetRecording(b active)                show/hide the stop button in the top bar
 *   signal StopRequested()                the stop button was clicked
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.mcrampon.Scopecam.Shell';
const OBJECT_PATH = '/io/github/mcrampon/Scopecam/Shell';
const IFACE = `
<node>
  <interface name="${BUS_NAME}">
    <method name="GetWindows">
      <arg type="u" direction="in" name="exclude_pid"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="GetPointer">
      <arg type="t" direction="in" name="window_id"/>
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="u" direction="out" name="mods"/>
      <arg type="(iiii)" direction="out" name="window_rect"/>
    </method>
    <method name="SetRecording">
      <arg type="b" direction="in" name="active"/>
    </method>
    <signal name="StopRequested"/>
  </interface>
</node>`;

const PICKABLE = [
    Meta.WindowType.NORMAL,
    Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG,
    Meta.WindowType.UTILITY,
];

class Service {
    constructor(onStopRequested) {
        this._indicator = null;
        this._timerId = 0;
        this._watchId = 0;
        this._onStopRequested = onStopRequested;
    }

    GetWindows(excludePid) {
        const workspace = global.workspace_manager.get_active_workspace();
        const tracker = Shell.WindowTracker.get_default();
        const windows = global.display.sort_windows_by_stacking(
            global.get_window_actors().map(a => a.meta_window)).reverse();
        const result = windows
            .filter(w => !w.minimized && w.showing_on_its_workspace() &&
                (w.is_on_all_workspaces() || w.get_workspace() === workspace) &&
                PICKABLE.includes(w.get_window_type()) && w.get_pid() !== excludePid)
            .map(w => {
                const r = w.get_frame_rect();
                return {
                    id: w.get_id(),
                    title: w.get_title() ?? '',
                    app: tracker.get_window_app(w)?.get_name() ?? '',
                    x: r.x, y: r.y, w: r.width, h: r.height,
                };
            });
        return JSON.stringify(result);
    }

    GetPointer(windowId) {
        const [x, y, mods] = global.get_pointer();
        let rect = [0, 0, 0, 0];
        if (windowId) {
            const actor = global.get_window_actors().find(a => a.meta_window.get_id() == windowId);
            if (actor) {
                const r = actor.meta_window.get_frame_rect();
                rect = [r.x, r.y, r.width, r.height];
            }
        }
        return [x, y, mods, rect];
    }

    SetRecordingAsync([active], invocation) {
        if (active)
            this._show(invocation.get_sender());
        else
            this._hide();
        invocation.return_value(null);
    }

    _show(sender) {
        this._hide();
        const button = new PanelMenu.Button(0.0, 'Scopecam recording', true);
        const box = new St.BoxLayout({style_class: 'scopecam-indicator'});
        box.add_child(new St.Icon({icon_name: 'media-record-symbolic', style_class: 'system-status-icon scopecam-dot'}));
        const label = new St.Label({text: '00:00', y_align: Clutter.ActorAlign.CENTER});
        box.add_child(label);
        button.add_child(box);
        button.connect('button-press-event', () => {
            this._hide();
            this._onStopRequested();
            return Clutter.EVENT_STOP;
        });
        Main.panel.addToStatusArea('scopecam-recording', button, 0, 'right');
        this._indicator = button;

        const start = GLib.get_monotonic_time();
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            const secs = Math.floor((GLib.get_monotonic_time() - start) / 1e6);
            const mm = String(Math.floor(secs / 60)).padStart(2, '0');
            const ss = String(secs % 60).padStart(2, '0');
            label.text = `${mm}:${ss}`;
            return GLib.SOURCE_CONTINUE;
        });
        // If the app goes away (crash, killed), don't leave a stale indicator behind.
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, sender, Gio.BusNameWatcherFlags.NONE,
            null, () => this._hide());
    }

    _hide() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
        this._indicator?.destroy();
        this._indicator = null;
    }

    destroy() {
        this._hide();
    }
}

export default class ScopecamExtension extends Extension {
    enable() {
        this._service = new Service(() => this._dbus?.emit_signal('StopRequested', null));
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this._service);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            null, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._nameId);
        this._dbus.unexport();
        this._service.destroy();
        this._service = null;
        this._dbus = null;
    }
}
