{
  description = "SpiriCamera development shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  # uv manages the Python environment itself; the shell only provides the tools.
  #   nix develop ./nix
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python3 pkgs.uv pkgs.gnumake ];
          env.UV_PYTHON_DOWNLOADS = "never";
        };
      });
    };
}
