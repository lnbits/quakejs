# Dedicated engine build

This directory records the native modifications to the pinned engine fork.
The shipped Linux x86-64 executable is statically linked with musl 1.2.5 and
GCC 15.2.0 using the pinned Nixpkgs revision in `native-shell.nix`. CMake 4.1.2
and Ninja 1.13.2 are build tools only. No Python packages are added.

For a clean checkout of the commit in source.json:

    node /path/to/quakejs/engine/patch.mjs /path/to/checkout
    node /path/to/quakejs/engine/patch-native.mjs /path/to/checkout
    cmake -S /path/to/checkout -B /path/to/build -G Ninja \
      -DBUILD_CLIENT=OFF -DBUILD_SERVER=ON -DBUILD_GAME_LIBRARIES=OFF \
      -DBUILD_GAME_QVMS=ON -DBUILD_STANDALONE=ON -DUSE_HTTP=OFF \
      -DUSE_VOIP=OFF -DUSE_CODEC_OPUS=OFF -DUSE_CODEC_VORBIS=OFF \
      -DUSE_OPENAL=OFF
    cmake --build /path/to/build -j4

The outputs are `Release/ioq3ded` and `Release/baseoa/vm/qagame.qvm`.
Build the portable executable separately, with game QVMs disabled:

    nix-shell engine/native-shell.nix --run \
      'bash engine/build-native.sh /path/to/checkout /path/to/static-build'

Copy `/path/to/static-build/Release/ioq3ded` into `bin/linux-x86_64/ioq3ded`
and update its SHA256 in the adjacent `manifest.json`. `readelf -l` must show
no INTERP segment and `readelf -d` must show no dynamic section. The native
executable is independent of the browser Emscripten toolchain. Replace `vm/qagame.qvm` in
`static/arena/baseoa/arena.pk3`, preserving all other archive entries, then update
the asset size and SHA256 in `static/arena/loader.js`. The game VM and the
corresponding source archive must accompany the executable.

The private protocol uses AF_UNIX/SOCK_SEQPACKET. Byte 0 is the message type;
byte 1 is the server-assigned peer (1–8, or 0 for server events).

- Parent → engine: 1 + peer + 48-byte life ID authorizes admission; 2 + peer +
  datagram carries gameplay; 3 + peer revokes admission administratively;
  7 + peer journals a no-winner forfeiture and revokes admission.
- Engine → parent: 1/3/7 acknowledge completed admission changes; 2 carries a
  game datagram; 4 hints at a journal event; 6 announces a loaded map.

Engine IPC and journals are private implementation details, never public API
inputs. Revocation acknowledgements are barriers: recovery processes recorded
deaths before releasing or reallocating a life. Native admissions are single-use;
reliable repeated connection packets cannot create a replacement character.

Native departures (including client commands and engine timeouts) also journal
forfeitures. A consumed guard prevents a frag, timeout and parent disconnect from
spending the same life twice. Administrative revocation and operator shutdown do
not charge an additional life.

The corresponding source archive includes the modified tree. Build that tree
directly; do not apply the patches a second time. For the original browser build,
see `browser-BUILD.md`; the native game QVM additionally removes projectiles when
their owner leaves. Repackage corresponding source after every engine change.

## Map selection

Arenas offer `aggressor`, `oa_dm7`, `oa_minia`, `czest1dm`, `oa_shine` and `kaos2`.
To reproduce the map-pack update from the release pinned in `source.json`, run:

    python engine/update-maps.py /path/to/openarena-0.8.8.zip

The script verifies the upstream archive hash, applies patch packs in load order,
replaces the map BSP/AAS files and level previews, includes referenced music, and
updates the loader size/hash. It preserves the patched game VMs. It updates the
game pack, not the native executable. OA DM1 and OA DM2 are no longer included.

The same build applies the four wall-logo placements in `logos/placements.json`
and embeds the supplied logo textures and shader. See `logos/README.md` for the
editable inputs and rendering-only changes.
