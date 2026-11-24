"""Compiler checks for the timeline board."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.models.rally import RallyPackage
from app.pipeline import board, library


def _png(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (16, 16), color).save(path)


class BoundariesTest(unittest.TestCase):
    def test_three_stills_over_ten_seconds(self) -> None:
        self.assertEqual(board.boundaries(3, 10), [0.0, 3.3, 6.7, 10.0])

    def test_empty_and_pinned_ends(self) -> None:
        self.assertEqual(board.boundaries(0, 10), [])
        edges = board.boundaries(2, 6.25)
        self.assertEqual(edges[0], 0.0)
        self.assertEqual(edges[-1], 6.2)


class SpanTest(unittest.TestCase):
    def test_clamp_swaps_and_keeps_a_minimum(self) -> None:
        self.assertEqual(board.clamp_span(0.5, 0.2), (0.2, 0.5))
        self.assertEqual(board.clamp_span(0.9, 0.95), (0.9, 0.95))
        self.assertEqual(board.clamp_span(0.98, 0.99), (0.96, 1.0))

    def test_snap_keyframe_grid(self) -> None:
        stamp = board.snap_keyframe(10 / 3, 10, [])
        self.assertIsNotNone(stamp)
        assert stamp is not None
        self.assertAlmostEqual(stamp, 10 / 3, places=4)
        self.assertIsNone(board.snap_keyframe(1.0, 0.5, []))
        self.assertIsNone(board.snap_keyframe(3.4, 10, [10 / 3]))


class CompileTest(unittest.TestCase):
    def test_storyboard_prompt_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {}
            for name, color in (
                ("s1", (20, 40, 80)),
                ("s2", (40, 80, 40)),
                ("s3", (80, 40, 20)),
                ("coat", (90, 70, 40)),
            ):
                path = root / f"{name}.png"
                _png(path, color)
                paths[name] = path
            assets = {
                key: {
                    "id": key,
                    "kind": "still",
                    "name": "coat" if key == "coat" else key,
                    "path": path,
                    "rel": path.name,
                }
                for key, path in paths.items()
            }
            story = {
                "v": 1,
                "duration": 10,
                "stills": [
                    {"id": "a", "asset_id": "s1"},
                    {"id": "b", "asset_id": "s2"},
                    {"id": "c", "asset_id": "s3"},
                ],
                "clips": [
                    {
                        "id": "sc1",
                        "kind": "scene",
                        "text": "rain on the glass",
                        "start": 0.0,
                        "end": 2 / 3,
                        "voice": "eve",
                        "refs": ["coat"],
                    },
                    {
                        "id": "sc2",
                        "kind": "scene",
                        "text": "the door opens",
                        "start": 2 / 3,
                        "end": 1.0,
                        "voice": "eve",
                        "refs": [],
                    },
                    {
                        "id": "v1",
                        "kind": "voice",
                        "text": "wait",
                        "start": 0.4,
                        "end": 0.55,
                        "voice": "eve",
                        "refs": [],
                    },
                    {
                        "id": "so1",
                        "kind": "sound",
                        "text": "traffic",
                        "start": 0.0,
                        "end": 1.0,
                        "voice": "eve",
                        "refs": [],
                    },
                ],
                "pins": [
                    {"id": "p1", "asset_id": "coat", "at": 0.16, "clip_id": "sc1"},
                ],
                "sequence": [],
                "pending": None,
                "error": "",
                "flash": "",
            }
            plan = board.plan_media(story, assets)
            self.assertEqual(plan["first"], paths["s1"])
            self.assertEqual(plan["last"], paths["s3"])
            self.assertEqual(len(plan["keyframes"]), 1)
            self.assertEqual(plan["keyframes"][0][0], paths["s2"])
            self.assertAlmostEqual(plan["keyframes"][0][1], 10 / 3, places=4)
            self.assertEqual(plan["references"], [paths["coat"]])
            self.assertEqual(plan["voices"], ["eve"])

            prompt = board.compile_prompt(story, assets, aspect="9:16", concept="Night lobby")
            for piece in (
                "Night lobby",
                "A 10 second clip, 9:16.",
                "0.0–3.3s. Still 1. This is the opening frame.",
                "3.3–6.7s. Still 2. This frame lands at 3.3s.",
                "6.7–10.0s. Still 3. This is the closing frame.",
                "0.0–6.7s. Scene: rain on the glass. Reference <IMAGE_0> (coat).",
                "6.7–10.0s. Scene: the door opens.",
                "4.0–5.5s. Voice eve (<AUDIO_0>): “wait”",
                "0.0–10.0s. Sound: traffic.",
            ):
                self.assertIn(piece, prompt)

    def test_typed_prefix_selects_voice_and_sound(self) -> None:
        story = {
            "v": 1,
            "duration": 6,
            "stills": [],
            "clips": [
                {
                    "id": "a",
                    "kind": "scene",
                    "text": "voice ara: hello",
                    "start": 0.1,
                    "end": 0.4,
                    "voice": "eve",
                    "refs": [],
                },
                {
                    "id": "b",
                    "kind": "scene",
                    "text": "sound: rain",
                    "start": 0.0,
                    "end": 1.0,
                    "voice": "eve",
                    "refs": [],
                },
                {
                    "id": "c",
                    "kind": "voice",
                    "text": "wait",
                    "start": 0.5,
                    "end": 0.7,
                    "voice": "leo",
                    "refs": [],
                },
                {
                    "id": "d",
                    "kind": "scene",
                    "text": "voice bob: hi",
                    "start": 0.0,
                    "end": 0.2,
                    "voice": "eve",
                    "refs": [],
                },
            ],
            "pins": [],
            "sequence": [],
            "pending": None,
            "error": "",
            "flash": "",
        }
        plan = board.plan_media(story, {})
        self.assertEqual(plan["voices"], ["ara", "leo"])
        prompt = board.compile_prompt(story, {}, aspect="16:9")
        self.assertIn('Voice ara (<AUDIO_0>): “hello”', prompt)
        self.assertIn("Sound: rain.", prompt)
        self.assertIn('Voice leo (<AUDIO_1>): “wait”', prompt)
        self.assertIn("Scene: voice bob: hi.", prompt)
        self.assertNotIn("Voice bob", prompt)


class EditorLineTest(unittest.TestCase):
    def test_typed_line_replaces_kind(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pkg = RallyPackage(
                name="line",
                concept="",
                aspect_ratio="16:9",
                duration_default=6,
            )
            pkg.root = root
            pkg.save()
            board.ensure_board(pkg, [])
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "voice", "text": "wait", "start": 0.2, "end": 0.5, "voice": "leo"},
                {},
            )
            clip = pkg.board["clips"][-1]
            self.assertEqual(clip["kind"], "voice")
            self.assertEqual(clip["text"], "wait")
            self.assertEqual(clip["voice"], "leo")
            board.apply_op(pkg, "patch_clip", {"id": clip["id"], "text": "voice ara: hello"}, {})
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["voice"], clip["text"]), ("voice", "ara", "hello"))
            board.apply_op(pkg, "patch_clip", {"id": clip["id"], "text": "the door"}, {})
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual(clip["kind"], "scene")
            self.assertEqual(clip["text"], "the door")
            board.apply_op(pkg, "patch_clip", {"id": clip["id"], "text": "sound: rain on glass"}, {})
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["text"]), ("sound", "rain on glass"))
            board.apply_op(pkg, "patch_clip", {"id": clip["id"], "text": "voice: good morning"}, {})
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["voice"], clip["text"]), ("voice", "eve", "good morning"))
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "voice eve: from the add", "start": 0.6, "end": 0.9},
                {},
            )
            added = pkg.board["clips"][-1]
            self.assertEqual((added["kind"], added["voice"], added["text"]), ("voice", "eve", "from the add"))

    def test_a_kind_choice_keeps_the_words(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pkg = RallyPackage(
                name="kind",
                concept="",
                aspect_ratio="16:9",
                duration_default=6,
            )
            pkg.root = root
            pkg.save()
            board.ensure_board(pkg, [])
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "the door", "start": 0.2, "end": 0.6, "voice": "eve"},
                {},
            )
            clip = pkg.board["clips"][-1]
            board.apply_op(
                pkg,
                "patch_clip",
                {"id": clip["id"], "kind": "voice", "voice": "sal", "text": "the door"},
                {},
            )
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["voice"], clip["text"]), ("voice", "sal", "the door"))
            board.apply_op(
                pkg,
                "patch_clip",
                {"id": clip["id"], "kind": "sound", "voice": "sal", "text": "rain on glass"},
                {},
            )
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["text"]), ("sound", "rain on glass"))
            board.apply_op(
                pkg,
                "patch_clip",
                {"id": clip["id"], "kind": "scene", "text": "voice ara: hello"},
                {},
            )
            clip = next(row for row in pkg.board["clips"] if row["id"] == clip["id"])
            self.assertEqual((clip["kind"], clip["voice"], clip["text"]), ("voice", "ara", "hello"))


class SeedTest(unittest.TestCase):
    def test_seed_saves_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _png(root / "start.png", (10, 20, 30))
            (root / "take.mp4").write_bytes(b"\x00" * 64)
            pkg = RallyPackage(
                name="seed",
                concept="A quiet office",
                aspect_ratio="9:16",
                duration_default=8,
            )
            pkg.root = root
            pkg.save()
            first = board.ensure_board(pkg)
            self.assertEqual(len(first["stills"]), 1)
            self.assertEqual(len(first["sequence"]), 1)
            self.assertEqual(first["clips"][0]["text"], "A quiet office")
            self.assertEqual(first["duration"], 8)
            meta = (root / "meta.json").read_text(encoding="utf-8")
            board.ensure_board(pkg)
            self.assertEqual((root / "meta.json").read_text(encoding="utf-8"), meta)
            rows = library.catalog(pkg)
            kinds = {row["rel"]: row["kind"] for row in rows}
            self.assertEqual(kinds.get("start.png"), "still")
            self.assertEqual(kinds.get("take.mp4"), "video")


class RemoveRefTest(unittest.TestCase):
    def test_chip_removes_one_still_and_the_line_takes_its_pins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library_dir = root / "library"
            library_dir.mkdir()
            _png(library_dir / "coat.png", (20, 30, 40))
            _png(library_dir / "lobby.png", (80, 40, 20))
            pkg = RallyPackage(
                name="refs",
                concept="",
                aspect_ratio="16:9",
                duration_default=6,
            )
            pkg.root = root
            pkg.board = {"v": 1}
            pkg.save()
            assets = library.by_id(pkg)
            coat = next(row for row in assets.values() if row["rel"].endswith("coat.png"))
            lobby = next(row for row in assets.values() if row["rel"].endswith("lobby.png"))
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "rain", "start": 0, "end": 0.5},
                assets,
            )
            board.apply_op(
                pkg,
                "add_clip",
                {"kind": "scene", "text": "door", "start": 0.5, "end": 1},
                assets,
            )
            rain = next(row for row in pkg.board["clips"] if row["text"] == "rain")
            door = next(row for row in pkg.board["clips"] if row["text"] == "door")
            board.apply_op(
                pkg,
                "add_pin",
                {"asset_id": coat["id"], "at": 0.2, "clip_id": rain["id"]},
                assets,
            )
            board.apply_op(
                pkg,
                "add_pin",
                {"asset_id": coat["id"], "at": 0.7, "clip_id": door["id"]},
                assets,
            )
            board.apply_op(
                pkg,
                "add_pin",
                {"asset_id": lobby["id"], "at": 0.8, "clip_id": door["id"]},
                assets,
            )
            board.apply_op(
                pkg,
                "remove",
                {"kind": "ref", "id": coat["id"], "clip_id": rain["id"]},
                assets,
            )
            rain = next(row for row in pkg.board["clips"] if row["id"] == rain["id"])
            self.assertNotIn(coat["id"], rain["refs"])
            self.assertFalse(
                any(
                    pin.get("clip_id") == rain["id"] and pin.get("asset_id") == coat["id"]
                    for pin in pkg.board["pins"]
                )
            )
            self.assertTrue(
                any(
                    pin.get("clip_id") == door["id"] and pin.get("asset_id") == coat["id"]
                    for pin in pkg.board["pins"]
                )
            )
            self.assertIn(lobby["id"], next(row["refs"] for row in pkg.board["clips"] if row["id"] == door["id"]))
            board.apply_op(pkg, "remove", {"kind": "clip", "id": door["id"]}, assets)
            self.assertTrue(any(row["id"] == rain["id"] for row in pkg.board["clips"]))
            self.assertFalse(any(row["id"] == door["id"] for row in pkg.board["clips"]))
            self.assertFalse(any(pin.get("clip_id") == door["id"] for pin in pkg.board["pins"]))
            self.assertFalse(any(pin.get("asset_id") == lobby["id"] for pin in pkg.board["pins"]))


if __name__ == "__main__":
    unittest.main()
