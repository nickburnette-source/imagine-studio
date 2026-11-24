"""Cut, join, and keep for the glass editor."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.models.rally import RallyPackage
from app.pipeline import board, edit, library
from app.pipeline.scenes import probe


def _clip(clip_id: str, asset_id: str, start: float, end: float, fps: float = 30) -> dict:
    return {
        "id": clip_id,
        "parts": [
            {"asset_id": asset_id, "start": start, "end": end, "fps": fps}
        ],
    }


def _color(path: Path, seconds: float, *, audio: bool) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x224466:s=320x180:d={seconds}:r=30",
    ]
    if audio:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
            ]
        )
    else:
        cmd.append("-an")
    cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    subprocess.run(cmd, check=True, capture_output=True)


class CutMathTest(unittest.TestCase):
    def test_cut_join_and_undo_restore_the_piece(self) -> None:
        session = {"clips": [_clip("a", "v", 0, 6)], "undo": []}
        edit.cut_at(session, 2)
        self.assertEqual(len(session["clips"]), 2)
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 2)
        self.assertEqual(session["clips"][1]["parts"][0]["start"], 2)
        left = session["clips"][0]["id"]
        edit.join_next(session, left)
        self.assertEqual(len(session["clips"]), 1)
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 6)
        edit.undo(session)
        self.assertEqual(len(session["clips"]), 2)
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 2)

    def test_a_gap_stays_two_parts(self) -> None:
        session = {
            "clips": [_clip("a", "v", 0, 2), _clip("b", "v", 4, 6)],
            "undo": [],
        }
        edit.join_next(session, "a")
        self.assertEqual(len(session["clips"]), 1)
        self.assertEqual(len(session["clips"][0]["parts"]), 2)

    def test_reorder_delete_and_empty_undo(self) -> None:
        session = {
            "clips": [_clip("a", "v", 0, 1), _clip("b", "w", 0, 1)],
            "undo": [],
        }
        edit.reorder(session, ["a", "b"])
        self.assertEqual(session["undo"], [])
        edit.reorder(session, ["b", "a"])
        self.assertEqual([clip["id"] for clip in session["clips"]], ["b", "a"])
        with self.assertRaises(ValueError):
            edit.reorder(session, ["b"])
        edit.delete_clip(session, "b")
        edit.delete_clip(session, "a")
        self.assertEqual(session["clips"], [])
        self.assertEqual(len(session["undo"]), 3)
        kept = edit.normalize_edit(session)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual(kept["clips"], [])
        edit.undo(session)
        self.assertEqual(session["clips"][0]["id"], "a")

    def test_trim_shortens_either_edge_and_undo_restores_it(self) -> None:
        session = {"clips": [_clip("a", "v", 0, 6)], "sounds": [], "undo": []}
        edit.trim_clip(session, "a", "out", 4, {})
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 4)
        edit.trim_clip(session, "a", "in", 1, {})
        self.assertEqual(session["clips"][0]["parts"][0]["start"], 1)
        edit.trim_clip(session, "a", "out", 99, {})
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 4)
        self.assertEqual(len(session["undo"]), 2)
        edit.undo(session)
        self.assertEqual(session["clips"][0]["parts"][0]["start"], 0)
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 4)
        edit.undo(session)
        self.assertEqual(session["clips"][0]["parts"][0]["end"], 6)

    def test_trimming_a_sound_keeps_its_out_point(self) -> None:
        session = {
            "clips": [_clip("a", "v", 0, 6)],
            "sounds": [{"id": "s", "asset_id": "wav", "start": 0, "end": 4, "at": 1}],
            "undo": [],
        }
        edit.trim_sound(session, "s", "in", 1, {})
        sound = session["sounds"][0]
        self.assertEqual(sound["start"], 1)
        self.assertEqual(sound["at"], 2)
        self.assertEqual(sound["end"], 4)
        edit.trim_sound(session, "s", "out", 3, {})
        self.assertEqual(session["sounds"][0]["end"], 3)
        self.assertEqual(session["sounds"][0]["at"], 2)
        edit.undo(session)
        self.assertEqual(session["sounds"][0]["end"], 4)

    def test_edges_and_a_missing_take(self) -> None:
        session = {"clips": [_clip("a", "v", 0, 6)], "undo": []}
        with self.assertRaises(ValueError):
            edit.cut_at(session, 0.01)
        with self.assertRaises(ValueError):
            edit.join_next(session, "a")
        with self.assertRaises(ValueError):
            edit.undo(session)
        story = {"sequence": [], "pending": None}
        with self.assertRaisesRegex(ValueError, "Generate a take"):
            edit.open_edit(story, {}, {})

    def test_normalize_leaves_a_board_without_a_cut(self) -> None:
        clean = board.normalize({"v": 1, "duration": 6})
        self.assertNotIn("edit", clean)
        self.assertIsNone(edit.normalize_edit(None))
        self.assertIsNone(edit.normalize_edit({"clips": [], "undo": []}))
        shown = edit.present({"clips": [], "undo": [[{"id": "a", "parts": [{"asset_id": "v", "start": 0, "end": 1, "fps": 30}]}]]}, {}, lambda _row: "")
        self.assertEqual(shown["clips"], [])
        self.assertEqual(shown["undo"], 1)


class ReferenceTest(unittest.TestCase):
    def test_a_still_lands_on_the_line_under_it(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "coat.png"
            Image.new("RGB", (8, 8), (40, 30, 20)).save(image)
            pkg = RallyPackage(name="refs", concept="", aspect_ratio="16:9", duration_default=10)
            pkg.root = root
            pkg.board = {
                "v": 1,
                "duration": 10,
                "stills": [],
                "clips": [],
                "pins": [],
                "sequence": [],
                "pending": None,
                "error": "",
                "flash": "",
            }
            pkg.save()
            still = library.ingest(pkg, image, name="Coat", kind="still")
            assets = library.by_id(pkg)
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "rain on the glass", "start": 0.2, "end": 0.6},
                assets,
            )
            clip_id = pkg.board["clips"][0]["id"]
            board.apply_op(pkg, "add_pin", {"asset_id": still["id"], "at": 0.1}, assets)
            pin = pkg.board["pins"][0]
            self.assertEqual(pin["clip_id"], clip_id)
            self.assertIn(still["id"], pkg.board["clips"][0]["refs"])
            prompt = board.compile_prompt(pkg.board, assets, aspect="16:9")
            self.assertIn("Scene: rain on the glass", prompt)
            self.assertIn("Reference <IMAGE_0> (Coat)", prompt)
            board.apply_op(pkg, "remove", {"kind": "pin", "id": pin["id"]}, assets)
            self.assertEqual(pkg.board["pins"], [])
            self.assertEqual(pkg.board["clips"][0]["refs"], [])

    def test_moving_a_pin_follows_the_line(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "lamp.png"
            Image.new("RGB", (8, 8), (20, 20, 40)).save(image)
            pkg = RallyPackage(name="refs", concept="", aspect_ratio="16:9", duration_default=10)
            pkg.root = root
            pkg.board = {
                "v": 1,
                "duration": 10,
                "stills": [],
                "clips": [],
                "pins": [],
                "sequence": [],
                "pending": None,
                "error": "",
                "flash": "",
            }
            pkg.save()
            still = library.ingest(pkg, image, name="Lamp", kind="still")
            assets = library.by_id(pkg)
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "rain", "start": 0.0, "end": 0.4},
                assets,
            )
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "door", "start": 0.6, "end": 1.0},
                assets,
            )
            first, second = pkg.board["clips"]
            board.apply_op(pkg, "add_pin", {"asset_id": still["id"], "at": 0.1}, assets)
            pin = pkg.board["pins"][0]
            self.assertEqual(pin["clip_id"], first["id"])
            board.apply_op(pkg, "patch_pin", {"id": pin["id"], "at": 0.8}, assets)
            self.assertEqual(pkg.board["pins"][0]["clip_id"], second["id"])
            self.assertEqual(pkg.board["clips"][0]["refs"], [])
            self.assertEqual(pkg.board["clips"][1]["refs"], [still["id"]])

    def test_a_loose_pin_is_tied_when_the_board_opens(self) -> None:
        raw = {
            "v": 1,
            "duration": 10,
            "clips": [
                {
                    "id": "rain",
                    "kind": "scene",
                    "text": "rain",
                    "start": 0.0,
                    "end": 0.6667,
                    "voice": "eve",
                    "refs": [],
                },
                {
                    "id": "door",
                    "kind": "scene",
                    "text": "door",
                    "start": 0.6667,
                    "end": 1.0,
                    "voice": "eve",
                    "refs": [],
                },
            ],
            "pins": [
                {"id": "loose", "asset_id": "lamp", "at": 0.6661, "clip_id": ""},
            ],
        }
        cleaned = board.normalize(raw)
        self.assertEqual(cleaned["pins"][0]["clip_id"], "door")
        self.assertEqual(cleaned["clips"][1]["refs"], ["lamp"])
        self.assertEqual(cleaned["clips"][0]["refs"], [])


class KeepTest(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg is not on PATH")

    def _project(self, root: Path) -> tuple[RallyPackage, dict[str, dict]]:
        pkg = RallyPackage(
            name="edit-keep",
            concept="",
            aspect_ratio="16:9",
            duration_default=6,
        )
        pkg.root = root
        pkg.board = {
            "v": 1,
            "duration": 6,
            "stills": [],
            "clips": [],
            "pins": [],
            "sequence": [],
            "pending": None,
            "error": "",
            "flash": "",
        }
        pkg.save()
        return pkg, {}

    def test_keep_writes_a_new_take_and_keeps_audio(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            _color(src, 2, audio=True)
            pkg, _assets = self._project(root)
            made = library.ingest(pkg, src, name="Lobby take", kind="video")
            assets = library.by_id(pkg)
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            self.assertEqual(len(pkg.board["edit"]["clips"]), 1)
            meta = (root / "meta.json").read_text(encoding="utf-8")
            board.ensure_board(pkg)
            self.assertEqual((root / "meta.json").read_text(encoding="utf-8"), meta)
            original = assets[made["id"]]["path"]
            board.apply_op(pkg, "edit_cut", {"at": 1.0}, assets)
            self.assertEqual(len(pkg.board["edit"]["clips"]), 2)
            board.apply_op(pkg, "edit_keep", {}, assets)
            catalog = library.by_id(pkg)
            pieces = [catalog[item["asset_id"]] for item in pkg.board["sequence"]]
            self.assertEqual([row["name"] for row in pieces], ["0–1s", "1–2s"])
            durations = [probe(row["path"]) for row in pieces]
            self.assertTrue(all(info["audio"] for info in durations))
            self.assertEqual(
                [round(info["duration"]) for info in durations],
                [1, 1],
            )
            self.assertTrue(original.is_file())
            self.assertEqual(pkg.board["flash"], pieces[0]["id"])
            self.assertTrue(pkg.board["edit"]["clips"])
            board.apply_op(pkg, "edit_discard", {}, assets)
            self.assertNotIn("edit", pkg.board)
            again = (root / "meta.json").read_text(encoding="utf-8")
            board.ensure_board(pkg)
            self.assertEqual((root / "meta.json").read_text(encoding="utf-8"), again)

    def test_a_whole_file_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "whole.mp4"
            _color(src, 2, audio=False)
            pkg, _assets = self._project(root)
            made = library.ingest(pkg, src, name="Take", kind="video")
            assets = library.by_id(pkg)
            before = {path.name for path in library.library_dir(pkg).iterdir()}
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            board.apply_op(pkg, "edit_keep", {}, assets)
            after = {path.name for path in library.library_dir(pkg).iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(
                [item["asset_id"] for item in pkg.board["sequence"]],
                [made["id"]],
            )
            info = probe(assets[made["id"]]["path"])
            self.assertFalse(info["audio"])

    def test_a_short_piece_drops_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "long.mp4"
            _color(src, 2, audio=False)
            pkg, _assets = self._project(root)
            made = library.ingest(pkg, src, name="Take", kind="video")
            assets = library.by_id(pkg)
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            board.apply_op(pkg, "edit_cut", {"at": 1.0}, assets)
            right = pkg.board["edit"]["clips"][1]["id"]
            board.apply_op(pkg, "edit_delete", {"id": right}, assets)
            board.apply_op(pkg, "edit_keep", {}, assets)
            kept = next(
                row for row in library.catalog(pkg)
                if row["kind"] == "video" and row["id"] != made["id"]
            )
            info = probe(kept["path"])
            self.assertEqual(kept["name"], "0–1s")
            self.assertAlmostEqual(info["duration"], 1.0, delta=0.15)
            self.assertFalse(info["audio"])

    def test_yank_lifts_audio_and_keep_mixes_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            _color(src, 2, audio=True)
            pkg, _assets = self._project(root)
            made = library.ingest(pkg, src, name="Lobby take", kind="video")
            assets = library.by_id(pkg)
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            clip_id = pkg.board["edit"]["clips"][0]["id"]
            board.apply_op(pkg, "edit_yank", {"id": clip_id}, assets)
            part = pkg.board["edit"]["clips"][0]["parts"][0]
            self.assertTrue(part["mute"])
            self.assertEqual(len(pkg.board["edit"]["sounds"]), 1)
            self.assertEqual(pkg.board["edit"]["sounds"][0]["asset_id"], made["id"])
            board.apply_op(pkg, "edit_undo", {}, assets)
            self.assertFalse(pkg.board["edit"]["clips"][0]["parts"][0].get("mute"))
            self.assertEqual(pkg.board["edit"]["sounds"], [])
            board.apply_op(pkg, "edit_yank", {"id": clip_id}, assets)
            board.apply_op(pkg, "edit_keep", {}, assets)
            kept = next(
                row for row in library.catalog(pkg)
                if row["kind"] == "video" and row["id"] != made["id"]
            )
            info = probe(kept["path"])
            self.assertEqual(kept["name"], "0–2s")
            self.assertAlmostEqual(info["duration"], 2.0, delta=0.2)
            self.assertTrue(info["audio"])
            self.assertTrue(made["path"].is_file())

    def test_two_cuts_break_out_as_three_takes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            _color(src, 3, audio=True)
            pkg, _assets = self._project(root)
            made = library.ingest(pkg, src, name="Lobby take", kind="video")
            assets = library.by_id(pkg)
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            board.apply_op(pkg, "edit_cut", {"at": 1.0}, assets)
            board.apply_op(pkg, "edit_cut", {"at": 2.0}, assets)
            self.assertEqual(len(pkg.board["edit"]["clips"]), 3)
            middle = pkg.board["edit"]["clips"][1]["id"]
            board.apply_op(pkg, "edit_keep", {"id": middle}, assets)
            catalog = library.by_id(pkg)
            pieces = [catalog[item["asset_id"]] for item in pkg.board["sequence"]]
            by_name = {row["name"]: row for row in pieces}
            self.assertEqual([row["name"] for row in pieces], ["0–1s", "1–2s", "2–3s"])
            for name in ("0–1s", "1–2s", "2–3s"):
                info = probe(by_name[name]["path"])
                self.assertAlmostEqual(info["duration"], 1.0, delta=0.2)
                self.assertTrue(info["audio"])
            self.assertEqual(
                [item["asset_id"] for item in pkg.board["sequence"]],
                [by_name["0–1s"]["id"], by_name["1–2s"]["id"], by_name["2–3s"]["id"]],
            )
            self.assertEqual(pkg.board["flash"], by_name["1–2s"]["id"])
            self.assertTrue(made["path"].is_file())


class SpeakTest(unittest.TestCase):
    def _project(self, root: Path) -> RallyPackage:
        pkg = RallyPackage(
            name="edit-speak",
            concept="",
            aspect_ratio="16:9",
            duration_default=6,
        )
        pkg.root = root
        pkg.board = {
            "v": 1,
            "duration": 6,
            "stills": [],
            "clips": [],
            "pins": [],
            "sequence": [],
            "pending": None,
            "error": "",
            "flash": "",
        }
        pkg.save()
        return pkg

    def test_a_bad_voice_never_synthesizes(self) -> None:
        from app.pipeline import speech

        def boom(text: str, voice: str) -> bytes:
            raise AssertionError(text or voice)

        speech._SYNTHESIZER = boom
        try:
            with self.assertRaises(ValueError):
                with tempfile.TemporaryDirectory() as raw:
                    pkg = self._project(Path(raw))
                    import asyncio

                    asyncio.run(speech.place_on_lane(pkg, text="Hi", voice="bob", at=0))
        finally:
            speech._SYNTHESIZER = None

    def test_an_empty_line_never_synthesizes(self) -> None:
        import asyncio

        from app.pipeline import speech

        speech._SYNTHESIZER = lambda text, voice: speech.silent_wav(text)
        try:
            with tempfile.TemporaryDirectory() as raw:
                pkg = self._project(Path(raw))
                with self.assertRaises(ValueError):
                    asyncio.run(speech.place_on_lane(pkg, text="   ", voice="eve", at=0))
        finally:
            speech._SYNTHESIZER = None

    def test_a_spoken_line_lands_on_the_lane(self) -> None:
        import asyncio

        from app.pipeline import speech

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg is not on PATH")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            _color(src, 2, audio=False)
            pkg = self._project(root)
            made = library.ingest(pkg, src, name="Take", kind="video")
            assets = library.by_id(pkg)
            board.apply_op(pkg, "edit_open", {"asset_id": made["id"]}, assets)
            speech._SYNTHESIZER = lambda text, voice: speech.silent_wav("Wait here")
            try:
                asyncio.run(
                    speech.place_on_lane(pkg, text="  Wait here  ", voice="Ara", at=1.25)
                )
            finally:
                speech._SYNTHESIZER = None
            sounds = pkg.board["edit"]["sounds"]
            self.assertEqual(len(sounds), 1)
            self.assertAlmostEqual(sounds[0]["at"], 1.25, places=3)
            self.assertFalse(pkg.board["edit"]["clips"][0]["parts"][0].get("mute"))
            row = library.by_id(pkg)[sounds[0]["asset_id"]]
            self.assertEqual(row["kind"], "audio")
            self.assertEqual(row["name"], "ara · Wait here")
            self.assertGreater(edit._audio_duration(row["path"]), 0.4)
            self.assertEqual(pkg.board["flash"], row["id"])
            self.assertEqual(pkg.board["error"], "")


if __name__ == "__main__":
    unittest.main()
