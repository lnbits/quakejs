"""Build a verified LNbits extension ZIP without local data or LFS pointers."""

import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def release_files(root):
    excluded = {"dev", "__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if excluded.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing a symlink in the release: {relative}")
        if not path.is_file():
            continue
        if (
            path.name.startswith(".env")
            or path.name == ".lnbits_auth_key"
            or path.suffix.lower()
            in {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}
        ):
            raise ValueError(
                f"Refusing local data or credentials in release: {relative}"
            )
        with path.open("rb") as source:
            if source.read(64).startswith(
                b"version https://git-lfs.github.com/spec/v1"
            ):
                raise ValueError(
                    f"Missing LFS content: {relative}. Run git lfs pull first."
                )
        files.append(path)
    return files


def verify_artifacts(root):
    for name in (
        "static/arena/engine.js",
        "static/arena/baseoa/arena.pk3",
        "static/sources/engine-source.js",
        "static/sources/asset-source.js",
        "static/share/template.png",
        "static/share/lobby.png",
        "static/share/DejaVuSansCondensed-Bold.ttf",
        "static/share/FONT-LICENSE.txt",
        "LICENSE",
    ):
        if not (root / name).is_file():
            raise ValueError(f"Missing required release file: {name}")
    binary = root / "bin/linux-x86_64/ioq3ded"
    manifest = json.loads(binary.with_name("manifest.json").read_text())
    with binary.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != manifest["sha256"]:
            raise ValueError("Native engine checksum does not match its manifest.")
    loader = (root / "static/arena/loader.js").read_text()
    metadata = re.search(r"size:\s*(\d+),\s*sha256:\s*'([a-f0-9]{64})'", loader)
    if not metadata:
        raise ValueError("Missing game asset size/checksum in loader manifest.")
    assets = root / "static/arena/baseoa/arena.pk3"
    if assets.stat().st_size != int(metadata[1]):
        raise ValueError("Game asset size does not match its loader manifest.")
    with assets.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != metadata[2]:
            raise ValueError("Game asset checksum does not match its loader manifest.")
    with ZipFile(assets) as archive:
        for name in (
            "maps/aggressor.bsp",
            "maps/oa_dm7.bsp",
            "maps/oa_minia.bsp",
            "maps/czest1dm.bsp",
            "maps/oa_shine.bsp",
            "maps/kaos2.bsp",
            "textures/quakejs_logos/bitfest.tga",
            "textures/quakejs_logos/lnbits.tga",
            "scripts/quakejs_logos.shader",
            "vm/qagame.qvm",
            "vm/cgame.qvm",
            "vm/ui.qvm",
        ):
            if name not in archive.namelist():
                raise ValueError(f"Game asset pack is missing {name}.")


def main():
    root = Path(__file__).resolve().parents[1]
    files = release_files(root)
    verify_artifacts(root)
    version = json.loads((root / "config.json").read_text())["version"]
    output = root / "dev" / f"quakejs-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    try:
        with ZipFile(
            temporary, "w", compression=ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in files:
                archive.write(path, str(Path("quakejs") / path.relative_to(root)))
        with ZipFile(temporary) as archive:
            if archive.testzip():
                raise RuntimeError("Release ZIP integrity check failed")
        with temporary.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        temporary.replace(output)
        output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n")
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Packaged {output.name}: {output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
