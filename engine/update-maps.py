"""Add the selected maps from the pinned OpenArena release to the existing pack.

Usage: python engine/update-maps.py /path/to/openarena-0.8.8.zip
Preserves patched VMs and replaces the selectable map set.
"""

import hashlib
import json
import re
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from logo_maps import brand_map
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MAPS = ("aggressor", "oa_dm7", "oa_minia", "czest1dm", "oa_shine", "kaos2")


def read_maps(source: Path):
    pinned = json.loads((ROOT / "engine/source.json").read_text())
    with source.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != pinned["assetsSha256"]:
            raise ValueError("OpenArena release checksum does not match source.json.")
    wanted = {
        *(f"maps/{name}.{suffix}" for name in MAPS for suffix in ("bsp", "aas")),
        *(f"levelshots/{name}.{suffix}" for name in MAPS for suffix in ("jpg", "tga")),
        # Referenced by the czest1dm and oa_minia worldspawn entities.
        "music/OA09.ogg",
        "music/OA06.ogg",
    }
    additions = {}
    with ZipFile(source) as release:
        # Quake loads patch packs last; later entries override earlier versions.
        for name in sorted(release.namelist()):
            if "/baseoa/" not in name or not name.endswith(".pk3"):
                continue
            with ZipFile(BytesIO(release.read(name))) as pack:
                for item in pack.infolist():
                    if item.filename in wanted:
                        additions[item.filename] = pack.read(item)
    for name in MAPS:
        if f"maps/{name}.bsp" not in additions or f"maps/{name}.aas" not in additions:
            raise ValueError(f"Missing required map data: {name}")
    return additions


def update_maps(source: Path):
    additions = read_maps(source)
    placements = json.loads((ROOT / "engine/logos/placements.json").read_text())
    for name in MAPS:
        key = f"maps/{name}.bsp"
        additions[key] = brand_map(additions[key], placements[name])
    for logo in ("bitfest", "lnbits"):
        # Lossless format conversion for the shipped renderer; preserve RGBA pixels.
        with Image.open(ROOT / f"engine/logos/{logo}.png") as image:
            texture = BytesIO()
            image.save(texture, "TGA")
            additions[f"textures/quakejs_logos/{logo}.tga"] = texture.getvalue()
    additions["scripts/quakejs_logos.shader"] = (
        ROOT / "engine/logos/quakejs_logos.shader"
    ).read_bytes()
    target = ROOT / "static/arena/baseoa/arena.pk3"
    temporary = target.with_suffix(".new")
    try:
        with ZipFile(target) as old, ZipFile(temporary, "w", ZIP_DEFLATED) as new:
            for item in old.infolist():
                if item.filename.startswith(("maps/", "levelshots/")):
                    continue
                if item.filename.startswith("textures/quakejs_logos/"):
                    continue
                if item.filename not in additions:
                    new.writestr(item, old.read(item))
            for name, data in sorted(additions.items()):
                new.writestr(name, data)
        with ZipFile(temporary) as result:
            if result.testzip() is not None:
                raise ValueError("Updated map archive failed its integrity check.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    with target.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    loader = ROOT / "static/arena/loader.js"
    content = loader.read_text()
    content = re.sub(r"maps:\[[^]]*\]", "maps:" + json.dumps(MAPS), content, count=1)
    content = re.sub(r"size:\d+", f"size:{target.stat().st_size}", content, count=1)
    content = re.sub(r"sha256:'[0-9a-f]+'", f"sha256:'{digest}'", content, count=1)
    loader.write_text(content)
    print(f"Updated {len(MAPS)} map choices; {target.stat().st_size} bytes; {digest}")


if __name__ == "__main__":
    update_maps(Path(sys.argv[1]))
