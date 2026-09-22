#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo 'Usage: build-native.sh PATCHED_SOURCE BUILD_DIRECTORY' >&2
  exit 2
fi
cmake -S "$1" -B "$2" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=x86_64-unknown-linux-musl-gcc \
  -DCMAKE_EXE_LINKER_FLAGS=-static \
  -DBUILD_CLIENT=OFF -DBUILD_SERVER=ON -DBUILD_GAME_LIBRARIES=OFF \
  -DBUILD_GAME_QVMS=OFF -DBUILD_STANDALONE=ON -DUSE_HTTP=OFF \
  -DUSE_VOIP=OFF -DUSE_CODEC_OPUS=OFF -DUSE_CODEC_VORBIS=OFF \
  -DUSE_OPENAL=OFF
cmake --build "$2" -j4
if readelf -l "$2/Release/ioq3ded" | grep -q INTERP; then
  echo 'Refusing a binary that needs a dynamic runtime' >&2
  exit 1
fi
sha256sum "$2/Release/ioq3ded"
