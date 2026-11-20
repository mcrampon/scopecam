{
  description = "Scopecam: screenshot and screencast tool (GNOME first, portals elsewhere)";

  inputs.nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        scopecam = pkgs.callPackage ./package.nix { };
        default = scopecam;
      });

      devShells = forAll (pkgs: {
        # `nix develop` then `python -m scopecam`: same runtime deps as the package.
        default = pkgs.mkShell {
          inputsFrom = [ self.packages.${pkgs.stdenv.hostPlatform.system}.scopecam ];
          shellHook = ''
            export GST_PLUGIN_SYSTEM_PATH_1_0="${self.packages.${pkgs.stdenv.hostPlatform.system}.scopecam.gstPluginPath}"
            export PATH="${pkgs.libinput.bin}/bin:$PATH"
          '';
        };
      });
    };
}
