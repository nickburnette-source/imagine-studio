import asyncio
import os

# Never bill Imagine from this smoke test, even if .env says cli.
os.environ["IMAGINE_GENERATE_BACKEND"] = "stub"

from nicegui import ui

from app.models import project as store

pytest_plugins = ["nicegui.testing.user_plugin"]


async def test_image_then_video(user) -> None:
    slug = "studio-smoke"
    if (store.OUTPUT_DIR / slug).exists():
        store.delete(slug)
    try:
        await user.open("/")
        await user.should_see("Imagine")
        await user.should_see("Create")
        user.find(ui.input).type("Studio smoke")
        user.find("Create").click()
        await asyncio.sleep(0.3)
        await user.should_see("Generate image")
        await user.should_see("Nothing generated yet.")

        user.find(kind=ui.button, content="Generate image").click()
        await asyncio.sleep(0.05)
        assert user.notify.contains("Write a prompt")

        area = next(iter(user.find(ui.textarea).elements))
        area.value = "a red housing bolted to the ceiling"
        user.find(kind=ui.button, content="1:1").click()
        await asyncio.sleep(0.2)
        proj = store.load(slug)
        assert proj.aspect_ratio == "1:1"

        area = next(iter(user.find(ui.textarea).elements))
        area.value = "a red housing bolted to the ceiling"
        user.find(kind=ui.button, content="Generate image").click()
        await user.should_not_see("Nothing generated yet.", retries=50)
        proj = store.load(slug)
        assert proj.status == "ready" and not proj.error, proj.error
        shown = proj.shown_file()
        assert shown is not None and shown.suffix == ".png"
        resp = await user.http_client.get(f"/media/{slug}/{proj.shown}")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

        user.find(kind=ui.button, content="Video").click()
        await asyncio.sleep(0.2)
        await user.should_see("Generate video")
        await user.should_see("LENGTH")
        await user.should_see("QUALITY")
        await user.should_see("1080p")
        user.find(ui.slider).trigger("change", args=8)
        assert store.load(slug).duration == 8

        area = next(iter(user.find(ui.textarea).elements))
        area.value = "the cables hang and the man speaks"
        user.find(kind=ui.button, content="Generate video").click()
        await user.should_see("Edit this clip", retries=50)
        proj = store.load(slug)
        assert proj.status == "ready" and not proj.error, (proj.status, proj.error)
        shown = proj.shown_file()
        assert shown is not None and shown.suffix == ".mp4"
        assert len(proj.takes) == 2

        await user.open("/")
        await user.should_see("Studio smoke")
        resp = await user.http_client.get("/media/nope/takes/missing.png")
        assert resp.status_code == 404
    finally:
        if (store.OUTPUT_DIR / slug).exists():
            store.delete(slug)
