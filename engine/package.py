"""Build an LNbits extension ZIP without local databases or build artifacts."""

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
version = json.loads((root / "config.json").read_text())["version"]
output = root / "dev" / f"quakejs-{version}.zip"
output.parent.mkdir(exist_ok=True)
excluded = {"dev", "__pycache__", ".pytest_cache", ".ruff_cache", ".git"}

with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if excluded.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError(f"Refusing a symlink in the release: {relative}")
        if path.is_file():
            archive.write(path, str(Path("quakejs") / relative))

with ZipFile(output) as archive:
    if archive.testzip():
        raise RuntimeError("Release ZIP integrity check failed")
digest = hashlib.sha256()
with output.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
output.with_suffix(".zip.sha256").write_text(f"{digest.hexdigest()}  {output.name}\n")
print(f"Packaged {output.name}: {output.stat().st_size:,} bytes")
