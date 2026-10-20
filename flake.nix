{
  description = "Dev shell for security-notebook-tools";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python314;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [ python pkgs.uv pkgs.ruff ];

          UV_PYTHON = "${python}/bin/python3.14";
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON_PREFERENCE = "only-system";
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];

          shellHook = ''
            if [ ! -d .venv ]; then
              uv sync
            fi
            if [ -f .venv/bin/activate ]; then
              source .venv/bin/activate
            fi
          '';
        };
      });
}
