let
  # Nixpkgs 26.05: Emscripten 5.0.6, Binaryen 129, CMake 4.1.2.
  pkgs = import (builtins.fetchTarball "https://github.com/NixOS/nixpkgs/archive/569d578509928497eddc3fdbf94a799027050be4.tar.gz") {};
in pkgs.mkShell { packages = with pkgs; [ emscripten cmake ninja unzip zip git curl nodejs ]; }
