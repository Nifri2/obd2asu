{
  description = "Python Template with Nix Flake";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs?ref=nixos-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          pyyaml
          numpy
          pynput
          sounddevice
          
        ]);
      in {
        devShells.default = pkgs.mkShell {
          nativeBuildInputs = [
            pkgs.python3
            pkgs.mpremote
          ];

          shellHook = ''
            fish --init-command 'source .dev-fish-setup.fish'
            exit 0
          ''; # Optional: Fish shell setup script, just delete if not needed

          buildInputs = [ pythonEnv ];
        };
      }
    );
}