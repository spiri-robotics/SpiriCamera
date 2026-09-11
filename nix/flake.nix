{
  description = "SpiriCamera development environment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      with pkgs;
      {
        devShell.default =
          mkShell {
            buildInputs = [ python313 uv pytest pytest-asyncio pytest-cov ];
            PYTHONPATH = "\${PWD}/src";
          };
      }
    );
}
