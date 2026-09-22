# Browser engine

The browser engine and controls are reused from the WASM extension. It is
compiled to JavaScript using Emscripten 5.0.6 and Binaryen 129, pinned in
`shell.nix`, so it works with LNbits' content security policy.

For a clean checkout of the engine commit in `source.json`, apply `patch.mjs`.
The corresponding source archive already contains those modifications.

Inside `nix-shell engine/shell.nix`, run:

    export EM_CACHE=/path/to/writable-build/em-cache
    emcmake cmake -S /path/to/source -B /path/to/browser-build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_STANDALONE=ON -DBUILD_SERVER=OFF \
      -DUSE_OPENAL=OFF -DUSE_VOIP=OFF -DUSE_CODEC_OPUS=OFF
    cmake --build /path/to/browser-build -j4

The `Release/QuakeArena.js` output is `static/arena/engine.js`. The dedicated
server runs separately; the browser connects using `static/arena/transport.js`.
Keep the native-built `qagame.qvm` in the asset package as described in BUILD.md.
