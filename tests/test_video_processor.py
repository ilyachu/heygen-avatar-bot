import unittest
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bot.services.video_processor import (
    _uniform_letterbox_crop_from_image,
    convert_to_stories_video,
    convert_to_video_note,
)


class UniformLetterboxCropTests(unittest.TestCase):
    def padded_image(self, color):
        image = Image.new("RGB", (320, 568), color)
        pixels = image.load()
        for y in range(194, 374):
            for x in range(320):
                pixels[x, y] = ((x * 3 + y) % 180, (x + y * 2) % 180, (x * 2 + y * 3) % 180)
        return image

    def test_handles_black_white_and_colored_padding(self):
        for color in ((0, 0, 0), (246, 246, 246), (42, 65, 90)):
            with self.subTest(color=color):
                self.assertEqual("crop=320:176:0:196", _uniform_letterbox_crop_from_image(self.padded_image(color)))

    def test_keeps_uniform_portrait_background(self):
        self.assertIsNone(_uniform_letterbox_crop_from_image(Image.new("RGB", (320, 568), "white")))

    def test_keeps_different_top_and_bottom_backgrounds(self):
        image = self.padded_image("white")
        image.paste((20, 20, 20), (0, 374, 320, 568))
        self.assertIsNone(_uniform_letterbox_crop_from_image(image))

    def test_detects_light_bands_around_landscape_scene(self):
        image = Image.new("RGB", (320, 568), (246, 246, 246))
        pixels = image.load()
        for y in range(100, 468):
            for x in range(320):
                pixels[x, y] = ((x * 3 + y) % 180, (x + y * 2) % 180, (x * 2 + y * 3) % 180)

        self.assertEqual("crop=320:364:0:102", _uniform_letterbox_crop_from_image(image))

    def test_keeps_full_frame_when_borders_are_not_matching_padding(self):
        image = Image.new("RGB", (320, 568))
        pixels = image.load()
        for y in range(568):
            for x in range(320):
                pixels[x, y] = ((x * 3 + y) % 180, (x + y * 2) % 180, (x * 2 + y * 3) % 180)

        self.assertIsNone(_uniform_letterbox_crop_from_image(image))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class StoriesVideoIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def command(self, *args):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        self.assertEqual(0, process.returncode, stderr.decode(errors="replace")[-1000:])
        return stdout

    async def test_normalizes_portrait_landscape_and_padded_short_clips(self):
        cases = (
            ("portrait", "testsrc2=size=180x320:rate=10", None, "1"),
            ("landscape", "testsrc2=size=320x180:rate=10", None, "1"),
            ("light_padding", "testsrc2=size=320x180:rate=10", "pad=320:568:0:194:color=0xF6F6F6", "1"),
            ("black_padding", "testsrc2=size=320x180:rate=10", "pad=320:568:0:194:color=black", "1"),
            ("very_short_padding", "testsrc2=size=320x180:rate=10", "pad=320:568:0:194:color=white", "0.2"),
        )
        with tempfile.TemporaryDirectory() as folder, patch("bot.services.video_processor.settings.TEMP_DIR", folder):
            for name, source, padding, duration in cases:
                with self.subTest(name=name):
                    raw = str(Path(folder) / (name + ".mp4"))
                    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source,
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
                    if padding:
                        args.extend(["-vf", padding])
                    await self.command(*args, "-t", duration, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", raw)
                    ok, output, error = await convert_to_stories_video(raw)
                    self.assertTrue(ok, error)
                    probe = json.loads(await self.command("ffprobe", "-v", "error", "-show_streams", "-of", "json", output))
                    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
                    self.assertEqual((720, 1280, "1:1", "9:16"),
                                     (video["width"], video["height"], video["sample_aspect_ratio"], video["display_aspect_ratio"]))
                    self.assertTrue(any(stream["codec_type"] == "audio" for stream in probe["streams"]))
                    frame = str(Path(folder) / (name + ".png"))
                    await self.command("ffmpeg", "-v", "error", "-y", "-i", output, "-frames:v", "1", frame)
                    with Image.open(frame) as image:
                        self.assertIsNone(_uniform_letterbox_crop_from_image(image))
                    circle_ok, circle, circle_error = await convert_to_video_note(raw)
                    self.assertTrue(circle_ok, circle_error)
                    circle_probe = json.loads(await self.command("ffprobe", "-v", "error", "-show_streams", "-of", "json", circle))
                    circle_video = next(stream for stream in circle_probe["streams"] if stream["codec_type"] == "video")
                    self.assertEqual((640, 640, "1:1"), (circle_video["width"], circle_video["height"], circle_video["sample_aspect_ratio"]))
                    await self.command("ffmpeg", "-v", "error", "-y", "-i", circle, "-frames:v", "1", frame)
                    with Image.open(frame) as image:
                        self.assertIsNone(_uniform_letterbox_crop_from_image(image))
