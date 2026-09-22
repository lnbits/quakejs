let
  # Pinned build-only musl/GCC toolchain. Nothing from Nix is needed at runtime.
  pkgs = import (builtins.fetchTarball "https://github.com/NixOS/nixpkgs/archive/569d578509928497eddc3fdbf94a799027050be4.tar.gz") {};
in pkgs.mkShell {
  packages = [ pkgs.pkgsStatic.stdenv.cc pkgs.cmake pkgs.ninja pkgs.binutils ];
}
