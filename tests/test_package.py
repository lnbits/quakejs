import hashlib
import json
from zipfile import ZipFile

import pytest

from lnbits.extensions.quakejs.engine.package import release_files, verify_artifacts


def test_release_rejects_unhydrated_lfs_pointer(tmp_path):
    (tmp_path / "arena.pk3").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:test\nsize 194745636\n"
    )
    with pytest.raises(ValueError, match="git lfs pull"):
        release_files(tmp_path)


@pytest.mark.parametrize(
    "filename",
    [
        ".env",
        ".env.production",
        ".lnbits_auth_key",
        "wallet.sqlite3",
        "debug.log",
        "private.pem",
    ],
)
def test_release_refuses_accidental_local_data(tmp_path, filename):
    (tmp_path / filename).write_text("synthetic test data")
    with pytest.raises(ValueError, match="local data or credentials"):
        release_files(tmp_path)


def test_release_excludes_dev_and_git_and_rejects_symlinks(tmp_path):
    for folder in ("dev", ".git", "__pycache__"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / ".env").write_text("synthetic ignored data")
    code = tmp_path / "app.py"
    code.write_text("pass\n")
    assert release_files(tmp_path) == [code]
    (tmp_path / "linked.py").symlink_to(code)
    with pytest.raises(ValueError, match="symlink"):
        release_files(tmp_path)


@pytest.fixture
def artifacts(tmp_path):
    for name in (
        "static/arena/engine.js",
        "static/sources/engine-source.js",
        "static/sources/asset-source.js",
        "static/share/template.png",
        "static/share/lobby.png",
        "static/share/DejaVuSansCondensed-Bold.ttf",
        "static/share/FONT-LICENSE.txt",
        "LICENSE",
        "bin/linux-x86_64/ioq3ded",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (tmp_path / "bin/linux-x86_64/manifest.json").write_text(
        json.dumps({"sha256": hashlib.sha256(b"fixture").hexdigest()})
    )
    pack = tmp_path / "static/arena/baseoa/arena.pk3"
    pack.parent.mkdir()
    with ZipFile(pack, "w") as archive:
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
            archive.writestr(name, b"fixture")
    (tmp_path / "static/arena/loader.js").write_text(
        f"size:{pack.stat().st_size},sha256:'{hashlib.sha256(pack.read_bytes()).hexdigest()}'"
    )
    return tmp_path


def test_release_checks_required_content_and_integrity(artifacts):
    verify_artifacts(artifacts)
    pack = artifacts / "static/arena/baseoa/arena.pk3"
    data = bytearray(pack.read_bytes())
    data[-1] ^= 1
    pack.write_bytes(data)
    with pytest.raises(ValueError, match="checksum"):
        verify_artifacts(artifacts)
    pack.unlink()
    with pytest.raises(ValueError, match="Missing required"):
        verify_artifacts(artifacts)
