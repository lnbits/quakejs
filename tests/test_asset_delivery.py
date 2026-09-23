from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.staticfiles import StaticFiles

from lnbits.extensions.quakejs import quakejs_ext, views


def test_game_pack_bypasses_gzip_and_supports_caching_and_ranges(tmp_path, monkeypatch):
    pack = tmp_path / "baseoa" / "arena.pk3"
    pack.parent.mkdir()
    content = bytes(range(256)) * 1024  # Stream multiple FileResponse chunks.
    pack.write_bytes(content)
    monkeypatch.setattr(views, "game_assets", StaticFiles(directory=tmp_path))
    compressed = []
    original = GZipResponder.apply_compression

    def compress(self, body, **kwargs):
        compressed.append(len(body))
        return original(self, body, **kwargs)

    monkeypatch.setattr(GZipResponder, "apply_compression", compress)
    app = FastAPI()
    # LNbits registers static mounts before extension routes. Use that order.
    app.mount("/quakejs/static", StaticFiles(directory=tmp_path))
    app.include_router(quakejs_ext)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    url = "/quakejs/assets/arena.pk3?v=" + "a" * 64
    with TestClient(app, headers={"Accept-Encoding": "gzip"}) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-length"] == str(len(content))
        assert response.headers["content-encoding"] == "identity"
        assert response.headers["content-type"] == "application/octet-stream"
        assert (
            response.headers["cache-control"] == "public, max-age=86400, no-transform"
        )
        assert response.headers["accept-ranges"] == "bytes"
        head = client.head(url)
        assert head.status_code == 200 and not head.content
        assert head.headers == response.headers
        for headers in (
            {"If-None-Match": response.headers["etag"]},
            {"If-Modified-Since": response.headers["last-modified"]},
        ):
            cached = client.get(url, headers=headers)
            assert cached.status_code == 304 and not cached.content
            assert cached.headers["cache-control"] == response.headers["cache-control"]
        partial = client.get(url, headers={"Range": "bytes=100-999"})
        assert partial.status_code == 206 and partial.content == content[100:1000]
        assert partial.headers["content-range"] == f"bytes 100-999/{len(content)}"
        assert partial.headers["content-length"] == "900"
        assert partial.headers["content-encoding"] == "identity"
        resumed = client.get(
            url,
            headers={"Range": "bytes=100-999", "If-Range": response.headers["etag"]},
        )
        assert resumed.status_code == 206 and resumed.content == partial.content
        assert not compressed  # Not merely decompressed by TestClient: gzip never ran.
        assert client.get(url, headers={"Range": "bytes=9999999-"}).status_code == 416
        assert client.post(url).status_code == 405
        # Other static responses still use LNbits' normal compression middleware.
        compressed.clear()
        assert (
            client.get("/quakejs/static/baseoa/arena.pk3").headers["content-encoding"]
            == "gzip"
        )
        assert compressed


def test_game_pack_cannot_select_other_files_or_leak_storage_errors(
    tmp_path, monkeypatch
):
    (tmp_path / "secret.txt").write_text("DO-NOT-EXPOSE")
    monkeypatch.setattr(views, "game_assets", StaticFiles(directory=tmp_path))
    app = FastAPI(debug=True)
    app.include_router(quakejs_ext)
    with TestClient(app) as client:
        missing = client.get("/quakejs/assets/arena.pk3?path=../secret.txt")
        assert missing.status_code in (404, 503)
        assert "DO-NOT-EXPOSE" not in missing.text
        assert str(tmp_path) not in missing.text
        assert client.get("/quakejs/assets/secret.txt").status_code == 404

        async def broken(*args):
            raise OSError("DO-NOT-EXPOSE storage details")

        monkeypatch.setattr(views.game_assets, "get_response", broken)
        response = client.get("/quakejs/assets/arena.pk3")
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "DO-NOT-EXPOSE" not in response.text
