"""Bake four non-colliding logo decals into a Quake III BSP at build time."""

import struct

FACE = struct.Struct("<12i12f2i")
VERTEX = struct.Struct("<10f4B")
LEAF = struct.Struct("<12i")
MODEL = struct.Struct("<6f4i")
SHADER = struct.Struct("<64s2i")
LOGOS = {"bitfest": (350, 84), "lnbits": (350, 104)}


def records(data, layout):
    return [list(row) for row in layout.iter_unpack(data)]


def packed(rows, layout):
    return b"".join(layout.pack(*row) for row in rows)


def lumps(data):
    if data[:8] != b"IBSP\x2e\0\0\0":
        raise ValueError("Expected a Quake III version 46 BSP.")
    return [
        data[offset : offset + length]
        for offset, length in struct.iter_unpack("<2i", data[8:144])
    ]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def corners(placement):
    normal = placement["normal"]
    right = [-normal[1], normal[0], 0]
    width = placement["width"]
    aspect = LOGOS[placement["logo"]]
    height = width * aspect[1] / aspect[0]
    # A small physical offset plus polygonOffset prevents z-fighting.
    center = [x + n * 0.25 for x, n in zip(placement["center"], normal, strict=True)]
    return [
        [
            center[i] + right[i] * x * width / 2 + (y * height / 2 if i == 2 else 0)
            for i in range(3)
        ]
        for x, y in [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    ]


def inside_triangle(point, triangle):
    a, b, c = triangle
    v0 = [c[i] - a[i] for i in range(3)]
    v1 = [b[i] - a[i] for i in range(3)]
    v2 = [point[i] - a[i] for i in range(3)]
    aa, ab, bb = dot(v0, v0), dot(v0, v1), dot(v1, v1)
    denominator = aa * bb - ab * ab
    if abs(denominator) < 0.00001:
        return False
    u = (bb * dot(v0, v2) - ab * dot(v1, v2)) / denominator
    v = (aa * dot(v1, v2) - ab * dot(v0, v2)) / denominator
    return u >= -0.0001 and v >= -0.0001 and u + v <= 1.0001


def validate_placement(parts, placement):
    face = records(parts[13], FACE)[placement["surface"]]
    vertices = records(parts[10], VERTEX)[face[3] : face[3] + face[4]]
    indexes = list(struct.unpack(f"<{len(parts[11]) // 4}i", parts[11]))
    indexes = indexes[face[5] : face[5] + face[6]]
    triangles = [
        [vertices[index][:3] for index in indexes[i : i + 3]]
        for i in range(0, len(indexes), 3)
    ]
    normal = placement["normal"]
    if face[2] != 1 or abs(normal[2]) > 0.001 or dot(normal, face[21:24]) < 0.999:
        raise ValueError("Logo must face outward from a planar vertical wall.")
    plane = dot(vertices[0][:3], normal)
    if abs(dot(placement["center"], normal) - plane) > 0.01:
        raise ValueError("Logo center is not on its wall.")
    quad = corners(placement)
    # Sample the whole footprint, including edges, rather than just its center.
    for u in range(5):
        for v in range(5):
            point = [
                quad[0][i]
                + (quad[1][i] - quad[0][i]) * u / 4
                + (quad[3][i] - quad[0][i]) * v / 4
                for i in range(3)
            ]
            if not any(inside_triangle(point, triangle) for triangle in triangles):
                raise ValueError("Logo extends beyond its wall surface.")


def append_decals(parts, placements):
    faces = records(parts[13], FACE)
    vertices = records(parts[10], VERTEX)
    indexes = list(struct.unpack(f"<{len(parts[11]) // 4}i", parts[11]))
    shaders = records(parts[1], SHADER)
    models = records(parts[7], MODEL)
    insertion = models[0][6] + models[0][7]
    new_faces = []
    for placement in placements:
        if not models[0][6] <= placement["surface"] < insertion:
            raise ValueError("Logos must be attached to the static world.")
        validate_placement(parts, placement)
        shader = f"textures/quakejs_logos/{placement['logo']}".encode()
        if any(row[0].split(b"\0")[0] == shader for row in shaders):
            shader_index = next(
                i for i, row in enumerate(shaders) if row[0].split(b"\0")[0] == shader
            )
        else:
            shader_index = len(shaders)
            shaders.append([shader, 0, 0])
        face = [0] * 26
        face[:8] = [shader_index, -1, 1, len(vertices), 4, len(indexes), 6, -1]
        face[21:24] = placement["normal"]
        new_faces.append(face)
        for position, uv in zip(
            corners(placement), [(0, 1), (1, 1), (1, 0), (0, 0)], strict=True
        ):
            vertices.append(
                [*position, *uv, 0, 0, *placement["normal"], 255, 255, 255, 255]
            )
        # BSP draw indexes use clockwise winding viewed from the front.
        indexes.extend([0, 2, 1, 0, 3, 2])
    faces[insertion:insertion] = new_faces
    for model in models[1:]:
        if model[6] >= insertion:
            model[6] += len(placements)
    models[0][7] += len(placements)
    parts[1], parts[7] = packed(shaders, SHADER), packed(models, MODEL)
    parts[10], parts[13] = packed(vertices, VERTEX), packed(faces, FACE)
    parts[11] = struct.pack(f"<{len(indexes)}i", *indexes)
    return insertion


def link_leaves(parts, placements, insertion):
    leaves = records(parts[4], LEAF)
    old = list(struct.unpack(f"<{len(parts[5]) // 4}i", parts[5]))
    new = []
    linked = set()
    for leaf in leaves:
        surfaces = old[leaf[8] : leaf[8] + leaf[9]]
        mapped = [
            index + len(placements) if index >= insertion else index
            for index in surfaces
        ]
        for index, placement in enumerate(placements):
            if placement["surface"] in surfaces:
                mapped.append(insertion + index)
                linked.add(index)
        leaf[8], leaf[9] = len(new), len(mapped)
        new.extend(mapped)
    if len(linked) != len(placements):
        raise ValueError("A logo wall has no visibility leaves.")
    parts[4], parts[5] = packed(leaves, LEAF), struct.pack(f"<{len(new)}i", *new)


def brand_map(data, placements):
    if sorted(item["logo"] for item in placements) != [
        "bitfest",
        "bitfest",
        "lnbits",
        "lnbits",
    ]:
        raise ValueError("Each map must contain exactly two copies of each logo.")
    parts = lumps(data)
    if b"textures/quakejs_logos/" in parts[1]:
        raise ValueError("Apply logos to the original map, not an already branded BSP.")
    insertion = append_decals(parts, placements)
    link_leaves(parts, placements, insertion)
    header, body, offset = bytearray(data[:8]), bytearray(), 144
    for part in parts:
        header.extend(struct.pack("<2i", offset, len(part)))
        body.extend(part)
        padding = (-len(part)) % 4
        body.extend(b"\0" * padding)
        offset += len(part) + padding
    return bytes(header + body)
