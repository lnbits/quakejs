import json
import struct
from collections import Counter
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from lnbits.extensions.quakejs.engine.logo_maps import (
    FACE,
    LEAF,
    MODEL,
    SHADER,
    VERTEX,
    brand_map,
    lumps,
    packed,
    records,
)

ROOT = Path(__file__).resolve().parents[1]


def wall_fixture():
    parts = [b"" for _ in range(17)]
    parts[0] = b'{"classname" "worldspawn"}\0'
    parts[1] = SHADER.pack(b"textures/wall", 0, 1)
    parts[4] = LEAF.pack(0, 0, -1, -200, -10, 2200, 200, 200, 0, 4, 0, 0)
    parts[5] = struct.pack("<4i", 0, 1, 2, 3)
    parts[7] = MODEL.pack(-1, -200, -10, 2200, 200, 200, 0, 4, 0, 0)
    parts[7] += MODEL.pack(1900, -200, -10, 2200, 200, 200, 4, 1, 0, 0)
    vertices, faces, indexes, placements = [], [], [], []
    for index in range(5):
        face = [0] * 26
        face[:8] = [0, -1, 1, len(vertices), 4, len(indexes), 6, -1]
        face[21] = 1
        faces.append(face)
        for y, z in [(-128, 0), (128, 0), (128, 128), (-128, 128)]:
            vertices.append(
                [index * 500, y, z, 0, 0, 0, 0, 1, 0, 0, 255, 255, 255, 255]
            )
        indexes.extend([0, 2, 1, 0, 3, 2])
        if index < 4:
            placements.append(
                {
                    "logo": "bitfest" if index % 2 == 0 else "lnbits",
                    "surface": index,
                    "center": [index * 500, 0, 64],
                    "normal": [1, 0, 0],
                    "width": 200,
                }
            )
    parts[10], parts[13] = packed(vertices, VERTEX), packed(faces, FACE)
    parts[11] = struct.pack(f"<{len(indexes)}i", *indexes)
    header, body = bytearray(b"IBSP\x2e\0\0\0"), bytearray()
    for part in parts:
        header.extend(struct.pack("<2i", 144 + len(body), len(part)))
        body.extend(part)
    return bytes(header + body), placements


def test_decals_preserve_collision_gameplay_and_submodel_geometry():
    original, placements = wall_fixture()
    before = lumps(original)
    branded = brand_map(original, placements)
    after = lumps(branded)
    for index in (0, 2, 3, 6, 8, 9, 12, 14, 15, 16):
        assert before[index] == after[index]
    assert records(after[4], LEAF)[0][:8] == records(before[4], LEAF)[0][:8]
    models = records(after[7], MODEL)
    assert models[0][6:8] == [0, 8]
    assert models[1][6:8] == [8, 1]
    faces = records(after[13], FACE)
    assert faces[:4] == records(before[13], FACE)[:4]
    assert faces[8] == records(before[13], FACE)[4]
    assert after[10].startswith(before[10])
    assert set(struct.unpack("<8i", after[5])) == set(range(8))
    assert all(face[4] == 4 and face[6] == 6 for face in faces[4:8])
    with pytest.raises(ValueError, match="already branded"):
        brand_map(branded, placements)


def test_decals_refuse_incorrect_counts_and_oversized_wall_placements():
    original, placements = wall_fixture()
    with pytest.raises(ValueError, match="exactly two"):
        brand_map(original, placements[:3])
    placements[0]["width"] = 1000
    with pytest.raises(ValueError, match="beyond its wall"):
        brand_map(original, placements)


def test_shipped_maps_have_two_of_each_logo_and_preserve_original_logo_pixels():
    placements = json.loads((ROOT / "engine/logos/placements.json").read_text())
    with ZipFile(ROOT / "static/arena/baseoa/arena.pk3") as pack:
        for name in placements:
            parts = lumps(pack.read(f"maps/{name}.bsp"))
            shaders = records(parts[1], SHADER)
            counts = Counter(
                shaders[face[0]][0].split(b"\0")[0].decode()
                for face in records(parts[13], FACE)
                if b"textures/quakejs_logos/" in shaders[face[0]][0]
            )
            assert counts == {
                "textures/quakejs_logos/bitfest": 2,
                "textures/quakejs_logos/lnbits": 2,
            }
        for logo in ("bitfest", "lnbits"):
            with Image.open(ROOT / f"engine/logos/{logo}.png") as original:
                with Image.open(
                    BytesIO(pack.read(f"textures/quakejs_logos/{logo}.tga"))
                ) as converted:
                    assert original.size == converted.size
                    assert (
                        original.convert("RGBA").tobytes()
                        == converted.convert("RGBA").tobytes()
                    )
