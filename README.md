# Scopecam

Screenshots and screencasts for Linux desktops. GNOME first; other desktops through xdg-desktop-portal.

- **Screenshot** a region, a screen or a window. Saved as PNG and copied to the clipboard.
- **Screencast** a region, a screen or a window, with optional:
  - desktop audio and/or microphone (mixed into one track)
  - mouse pointer, with **click highlights** (ripple, colour per button)
  - **typed keys** shown at the bottom of the video (all keys, or shortcuts only)
- Save folders and file name patterns are configurable. The defaults match GNOME: `~/Pictures/Screenshots` and `~/Videos/Screencasts`.
- Encoding picked in the screencast options. Only encoders present on the machine are listed:
  - H.264 · MP4 (software, plays everywhere; the default)
  - H.264 or H.265 · MP4 (hardware, through VA-API)
  - VP8, VP9 or AV1 · WebM
  MP4 gets AAC audio, WebM gets Opus. 24, 30 or 60 fps.

## Usage

```
scopecam                       # capture overlay, always in screenshot mode, last region/screen/window
scopecam screenshot --region   # overlay preset to a mode (--screen on one monitor captures at once)
scopecam record --window       # same, in screencast mode; optional --delay N
scopecam toggle --region       # start recording, or stop the running one
scopecam stop
scopecam preferences
```

The overlay freezes the screen and shows a control panel at the bottom: screenshot or
screencast, region/screen/window, the shutter button, and the options, collapsed under
"Options".

In the overlay:
- Enter or Space captures.
- Ctrl+C captures and copies to the clipboard, even if automatic copying is off.
- Esc cancels.
- R, S and W switch to region, screen and window.
- P toggles the pointer.
- In screen and window modes, a click selects and a double-click captures.

The last region is preselected.

The delay applies after you confirm, and that capture is taken live.

Bind these to keys under Settings → Keyboard → Custom Shortcuts. For example, Print → `scopecam screenshot --region`, Shift+Print → `scopecam toggle --region`.

To stop a recording, use any of:
- the red ● timer in the top bar (needs the extension)
- the notification
- `scopecam stop`
- reopening the app window

## How it works

| | GNOME backend | Portal backend (any desktop) |
|---|---|---|
| Capture | `org.gnome.Mutter.ScreenCast` (RecordArea / RecordMonitor / RecordWindow), no dialogs | ScreenCast + Screenshot portals, using the system picker |
| Region / screen / window picking | Our own fullscreen picker over a frozen frame | Region: our picker, then crop. Window: system dialog |
| Click highlights | Pointer position from the Shell extension | Not available (Wayland hides the pointer) |
| Stop button | Top-bar indicator from the extension | Notification |

Keys and clicks are read with `libinput debug-events`. This needs read access to `/dev/input/event*`, which means being in the `input` group. They are drawn into the video by a GStreamer `cairooverlay` stage, using frame timestamps.

Recording pipeline: `pipewiresrc → crop/scale → videorate → cairooverlay → encoder → mp4mux/webmmux`, plus `pulsesrc` for the default sink monitor and/or the default microphone, mixed with `audiomixer`. MP4 is written fragmented while recording, so a crash still leaves a playable file, then finalised into a regular MP4.

### The GNOME Shell extension (`extension/scopecam@mcrampon`)

Optional, but it unlocks window picking, click highlights and the top-bar stop button. It exports `io.github.mcrampon.Scopecam.Shell` on the session bus:
- `GetWindows`
- `GetPointer`
- `SetRecording`
- signal `StopRequested`

Note that any app in your session can call it, and `GetWindows` exposes window titles.

The Nix package installs it into `share/gnome-shell/extensions`. Enable it with `gnome-extensions enable scopecam@mcrampon`, or through dconf. GNOME on Wayland only picks up a newly installed extension after you log out and back in.

## Development

```
nix develop
python -m scopecam            # run from the checkout
nix build                     # package (app + desktop file + extension)
```

Settings live in `~/.config/scopecam/config.json`. To test the portal backend on GNOME, set `SCOPECAM_BACKEND=portal`.

## Not done yet / ideas

- GIF export, audio device selection.
- Pause/resume, countdown overlay, a preview/annotate step after screenshots.

## License

[GPL-3.0-or-later](LICENSE)
