{
  lib,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  gst_all_1,
  pipewire,
  libinput,
}:

let
  gstPlugins = with gst_all_1; [
    gstreamer
    gst-plugins-base
    gst-plugins-good # pulsesrc, cairooverlay, mp4mux, vpx
    gst-plugins-bad # openh264, va hardware encoders
    gst-plugins-ugly # x264enc
    gst-libav # avenc_aac
    pipewire # pipewiresrc
  ];
  gstPluginPath = lib.makeSearchPathOutput "lib" "lib/gstreamer-1.0" gstPlugins;
in
python3Packages.buildPythonApplication {
  pname = "scopecam";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ python3Packages.setuptools ];

  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 libadwaita ] ++ gstPlugins;

  dependencies = with python3Packages; [ pygobject3 pycairo ];

  # Let wrapGAppsHook's wrapper be the only wrapper (avoids double wrapping).
  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --prefix PATH : ${lib.makeBinPath [ libinput ]}
      --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : ${gstPluginPath}
    )
  '';

  postInstall = ''
    install -Dm644 data/io.github.mcrampon.Scopecam.desktop -t $out/share/applications
    mkdir -p $out/share/gnome-shell/extensions
    cp -r extension/scopecam@mcrampon $out/share/gnome-shell/extensions/
  '';

  passthru = { inherit gstPluginPath; };

  meta = {
    description = "Screenshot and screencast tool for GNOME (and other desktops via portals)";
    mainProgram = "scopecam";
    platforms = lib.platforms.linux;
  };
}
