from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import FileSystemLoader
from PIL import Image

from lnbits.extensions.quakejs import crud, views
from lnbits.extensions.quakejs.share import HEIGHT, WIDTH, render_share_image


class Metadata(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "meta":
            self.tags[data.get("property") or data.get("name")] = data.get("content")


def test_social_crawlers_receive_public_metadata_and_matching_image(monkeypatch):
    # The extension Makefile runs from this folder; LNbits normally runs at root.
    monkeypatch.setattr(
        views.renderer.env,
        "loader",
        FileSystemLoader(str(Path(views.__file__).parent / "templates")),
    )

    async def lookup(sql, **kwargs):
        if kwargs["id"] not in ("cheap", "expensive"):
            return None
        return {
            "id": kwargs["id"],
            "entry_amount": 50 if kwargs["id"] == "cheap" else 100,
            "name": '<script>alert("arena")</script>',
            "haircut": 5,
            "map": "aggressor",
            "active": 1,
            "created_at": 0,
        }

    monkeypatch.setattr(crud, "one", lookup)
    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")
    images = []
    with TestClient(app, base_url="https://games.example") as client:
        for arena, amount, prize in (("cheap", 50, 9), ("expensive", 100, 19)):
            response = client.get(f"/quakejs/games/{arena}?private=do-not-share")
            assert response.status_code == 200
            tags = Metadata(response.text).tags
            assert tags["twitter:card"] == "summary_large_image"
            assert (
                tags["og:title"]
                == f"FIGHT ME IN QUAKE · SATS FOR KILLS · {amount} SATS TO JOIN"
            )
            assert f"Earn {prize} sats per kill" in tags["og:description"]
            assert tags["og:url"] == f"https://games.example/quakejs/games/{arena}"
            assert "private=" not in tags["og:image"]
            assert '<script>alert("arena")</script>' not in response.text
            assert tags["og:image"] == tags["twitter:image"]
            image = client.get(tags["og:image"])
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/jpeg"
            assert "public" in image.headers["cache-control"]
            decoded = Image.open(BytesIO(image.content))
            assert decoded.size == (WIDTH, HEIGHT)
            assert image.content == render_share_image(amount)
            # Query strings cannot override the arena's real entry price.
            assert (
                client.get(tags["og:image"] + "&amount=9999").content == image.content
            )
            images.append(image.content)
        assert images[0] != images[1]
        assert client.get("/quakejs/games/missing").status_code == 404
        assert client.get("/quakejs/games/missing/share.jpg").status_code == 404


def test_large_entry_price_still_renders_with_bounded_cache():
    data = render_share_image(1_000_000)
    assert Image.open(BytesIO(data)).size == (WIDTH, HEIGHT)
    assert len(data) < 1_000_000
    assert render_share_image.cache_info().maxsize == 32
