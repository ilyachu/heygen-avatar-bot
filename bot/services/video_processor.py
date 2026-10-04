import os
import uuid
import logging
import asyncio
import glob
import httpx
from PIL import Image, ImageOps
from typing import Optional, Tuple
from bot.config import settings

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _sync_composite_doctor(expert_key: str, bg_image_path: str, output_path: str) -> bool:
    try:
        canvas_w, canvas_h = 1600, 2400
        # Load background and fix mobile EXIF rotation
        bg = Image.open(bg_image_path)
        bg = ImageOps.exif_transpose(bg)
        bg = bg.convert('RGBA')
        bg_w, bg_h = bg.size

        # Aspect fill (cover) to canvas
        scale = max(canvas_w / bg_w, canvas_h / bg_h)
        new_w = int(bg_w * scale)
        new_h = int(bg_h * scale)
        bg_resized = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Center crop to 1600x2400
        left = (new_w - canvas_w) // 2
        top = (new_h - canvas_h) // 2
        bg_cropped = bg_resized.crop((left, top, left + canvas_w, top + canvas_h))

        # Doctor cutout path
        cutout_path = os.path.join(BASE_DIR, "bot", "assets", f"{expert_key}_cutout.png")
        if not os.path.exists(cutout_path):
            cutout_path = os.path.join("bot", "assets", f"{expert_key}_cutout.png")

        if not os.path.exists(cutout_path):
            logger.error(f"Doctor cutout not found at {cutout_path}")
            return False

        doctor = Image.open(cutout_path).convert('RGBA')
        bg_cropped.paste(doctor, (0, 0), doctor)

        final_rgb = bg_cropped.convert('RGB')
        final_rgb.save(output_path, "JPEG", quality=95)
        logger.info(f"Composited doctor on custom background: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Error in _sync_composite_doctor: {e}")
        return False

async def composite_doctor_on_background(expert_key: str, bg_image_path: str, output_path: str) -> bool:
    return await asyncio.to_thread(_sync_composite_doctor, expert_key, bg_image_path, output_path)

async def download_file(url: str, output_path: str) -> bool:
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.error(f"Failed to download video: HTTP {response.status_code}")
                    return False
                with open(output_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024*64):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return False

async def get_video_dimensions(file_path: str) -> Tuple[int, int, float]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "csv=s=x:p=0",
        file_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split()
        if lines:
            dims = lines[0].split("x")
            width = int(dims[0])
            height = int(dims[1])
            duration = float(lines[1]) if len(lines) > 1 else 0.0
            return width, height, duration
    except Exception as e:
        logger.warning(f"ffprobe dimension check failed: {e}")
    return 1280, 720, 0.0

async def convert_to_video_note(input_path: str) -> Tuple[bool, Optional[str], Optional[str]]:
    output_path = os.path.join(settings.TEMP_DIR, f"circle_{uuid.uuid4().hex[:8]}.mp4")
    
    width, height, _ = await get_video_dimensions(input_path)
    logger.info(f"Source video dimensions: {width}x{height}")
    padding_filter = await _detect_content_crop(input_path) if height > width else None
    if padding_filter:
        content_width, content_height, _, _ = map(int, padding_filter.split("=", 1)[1].split(":"))
        width, height = content_width, content_height
        logger.info("Circle padding crop applied: %s", padding_filter)

    # SMART CROPPING FORMULA:
    # 1. Landscape (16:9 or similar, e.g. 1280x720):
    #    The person is horizontally centered. Height is the limiting dimension.
    #    Crop square: height x height, x centered, y = 0 (top of the head).
    # 2. Portrait (9:16 or similar, e.g. 1080x1920 or 1536x2752):
    #    Width is the limiting dimension.
    #    The face is located in the upper 30-40% of the video!
    #    If we do a vertical center crop, the head gets cut off!
    #    Therefore, crop starts at y = (height - width) * 0.10 (capturing headroom + face + chest).
    # 3. Square (1:1):
    #    No crop needed, just resize to 640:640.

    if width > height:
        # Landscape (e.g. 1280x720, 1920x1080):
        # Preserve the full scene height so the forehead/chin stay in frame.
        crop_size = height - height % 2
        x_offset = int((width - crop_size) / 2)
        x_offset -= x_offset % 2
        y_offset = 0
        crop_filter = f"crop={crop_size}:{crop_size}:{x_offset}:{y_offset}"
    elif height > width:
        # Portrait (e.g. 720x1280 or 1080x1920):
        # Subject's head is located in the upper third.
        crop_size = width - width % 2
        y_offset = int((height - crop_size) * 0.10)
        y_offset -= y_offset % 2
        crop_filter = f"crop={crop_size}:{crop_size}:0:{y_offset}"
    else:
        # Already 1:1
        crop_filter = "null"

    filter_complex = f"{crop_filter},scale=640:640:flags=lanczos" if crop_filter != "null" else "scale=640:640:flags=lanczos"
    if padding_filter:
        filter_complex = f"{padding_filter},{filter_complex}"
    filter_complex += ",setsar=1"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", filter_complex,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-t", "60",
        output_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode()[-300:]
            logger.error(f"FFmpeg error: {err_msg}")
            return False, None, f"FFmpeg conversion failed: {err_msg}"

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True, output_path, None
        else:
            return False, None, "Output file not created"
    except Exception as e:
        logger.error(f"Exception in convert_to_video_note: {e}")
        return False, None, str(e)

def _uniform_letterbox_crop_from_image(image: Image.Image) -> Optional[str]:
    """Find large, matching uniform bands around a landscape scene.

    HeyGen can return a 9:16 file with a 16:9 scene padded by light gray
    bands. FFmpeg's cropdetect is designed around dark borders and keeps that
    padding, so use row uniformity. Only accept large matching
    bands that leave a landscape-sized content area; a normal portrait image
    should pass through unchanged.
    """
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size
    if width < 2 or height < 2:
        return None

    sample_x = range(0, width, max(1, width // 80))

    def row_mean_and_spread(y: int) -> tuple[tuple[float, float, float], float]:
        pixels = [image.getpixel((x, y)) for x in sample_x]
        mean = tuple(sum(pixel[channel] for pixel in pixels) / len(pixels) for channel in range(3))
        spread = max(
            max(pixel[channel] for pixel in pixels) - min(pixel[channel] for pixel in pixels)
            for channel in range(3)
        )
        return mean, float(spread)

    top_ref, top_spread = row_mean_and_spread(0)
    bottom_ref, bottom_spread = row_mean_and_spread(height - 1)

    def color_distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
        return max(abs(left[channel] - right[channel]) for channel in range(3))

    # Both borders must look like the same padding color. This avoids trimming
    # a portrait with one plain wall or a single uniform background band.
    if top_spread > 10 or bottom_spread > 10 or color_distance(top_ref, bottom_ref) > 18:
        return None

    def is_padding(y: int, reference: tuple[float, float, float]) -> bool:
        mean, spread = row_mean_and_spread(y)
        return spread <= 10 and color_distance(mean, reference) <= 18

    content_top = 0
    while content_top < height and is_padding(content_top, top_ref):
        content_top += 1
    content_bottom = height
    while content_bottom > content_top and is_padding(content_bottom - 1, bottom_ref):
        content_bottom -= 1

    content_height = content_bottom - content_top
    if (
        content_top < height * 0.08
        or height - content_bottom < height * 0.08
        or content_height >= height * 0.80
        or content_height > width * 1.20
    ):
        return None

    # Trim the anti-aliased transition row, keep yuv420p happy, and avoid a
    # visible one-pixel padding seam at the boundary.
    content_top += 2
    content_bottom -= 2
    content_top += content_top % 2
    content_bottom -= content_bottom % 2
    content_height = content_bottom - content_top
    if content_height < 2:
        return None
    return f"crop={width - width % 2}:{content_height}:0:{content_top}"


async def _detect_content_crop(input_path: str) -> Optional[str]:
    """Detect matching letterbox bands across representative frames.

    A single frame can contain a transient uniform wall or fade. Requiring the
    same crop on at least two frames makes detection conservative while
    still handling HeyGen's light-gray padding that ``cropdetect`` misses.
    """
    frame_pattern = os.path.join(settings.TEMP_DIR, f"cropcheck_{uuid.uuid4().hex[:8]}_%02d.png")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-frames:v", "3", "-vf", "select='eq(n,0)+eq(n,5)+eq(n,15)',scale=320:-2",
        "-vsync", "0", frame_pattern,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        frame_paths = sorted(glob.glob(frame_pattern.replace("%02d", "*")))
        if proc.returncode != 0 or not frame_paths:
            return None
        detections = []
        for frame_path in frame_paths:
            with Image.open(frame_path) as image:
                probe_frame_height = image.height
                detected = _uniform_letterbox_crop_from_image(image)
            if detected:
                _, probe = detected.split("=", 1)
                detections.append(tuple(int(value) for value in probe.split(":")))

        # A short clip may yield only one frame, but normal clips must agree on
        # at least two samples before we remove any content.
        if len(frame_paths) >= 2 and len(detections) < 2:
            return None
        if not detections:
            return None
        first_width, first_height, first_x, first_y = detections[0]
        if any(
            width != first_width
            or abs(height - first_height) > 4
            or abs(x - first_x) > 4
            or abs(y - first_y) > 4
            or abs((y + height) - (first_y + first_height)) > 4
            for width, height, x, y in detections
        ):
            return None

        # The helper sees the 320px probe width. Scale the stable crop back to
        # source dimensions before passing it to FFmpeg.
        source_width, source_height, _ = await get_video_dimensions(input_path)
        scale_y = source_height / probe_frame_height
        crop_width = source_width - source_width % 2
        crop_height = int(round(sum(item[1] for item in detections) / len(detections) * scale_y))
        crop_y = int(round(sum(item[3] for item in detections) / len(detections) * scale_y))
        crop_height -= crop_height % 2
        crop_y -= crop_y % 2
        if crop_y + crop_height > source_height:
            return None
        return f"crop={crop_width}:{crop_height}:0:{crop_y}"
    except Exception as e:
        logger.warning(f"uniform letterbox detection failed: {e}")
    finally:
        for frame_path in glob.glob(frame_pattern.replace("%02d", "*")):
            safe_remove(frame_path)
    return None


def _stories_fill_filters() -> list[str]:
    """Cover-fill into 720x1280 with square pixels (Telegram-safe)."""
    return [
        "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos",
        "crop=720:1280",
        "setsar=1",
    ]


async def convert_to_stories_video(input_path: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Normalize avatar video to vertical 9:16 for Stories / Reels.

    Portrait sources (photo avatars): cover fill.
    Landscape digital twins (desk): cover-fill center crop into 9:16.
    Letterboxed tall frames from HeyGen (9:16 request on landscape twin):
    strip bars first, then cover-fill. Always force setsar=1 so Telegram
    does not reinterpret a non-square SAR as landscape.
    """
    output_path = os.path.join(settings.TEMP_DIR, f"stories_{uuid.uuid4().hex[:8]}.mp4")
    width, height, _ = await get_video_dimensions(input_path)
    logger.info(f"Stories source video dimensions: {width}x{height}")

    aspect = (height / width) if width else 0
    vf_parts: list[str] = []

    # Tall frame that may already contain letterboxed landscape content.
    if aspect >= 1.4:
        detected = await _detect_content_crop(input_path)
        if detected:
            try:
                _, rest = detected.split("=", 1)
                cw, ch, _, _ = [int(float(x)) for x in rest.split(":")[:4]]
                if cw * ch < width * height * 0.92:
                    vf_parts.append(detected)
                    logger.info(f"Stories padding crop applied: {detected}")
            except Exception:
                pass
            vf_parts.extend(_stories_fill_filters())
        else:
            vf_parts.extend(_stories_fill_filters())
    else:
        vf_parts.extend(_stories_fill_filters())
    filter_complex = ",".join(vf_parts)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", filter_complex,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-t", "180",
        output_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode()[-300:]
            logger.error(f"FFmpeg stories error: {err_msg}")
            return False, None, f"FFmpeg stories conversion failed: {err_msg}"

        if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
            return False, None, "Output file not created"

        out_w, out_h, _ = await get_video_dimensions(output_path)
        logger.info(f"Stories output video dimensions: {out_w}x{out_h}")
        if out_w != 720 or out_h != 1280:
            safe_remove(output_path)
            return False, None, f"Stories output has wrong size: {out_w}x{out_h}"

        if await _detect_content_crop(output_path):
            safe_remove(output_path)
            return False, None, "Не удалось удалить поля HeyGen при адаптации в 9:16."

        return True, output_path, None
    except Exception as e:
        logger.error(f"Exception in convert_to_stories_video: {e}")
        return False, None, str(e)


async def convert_to_voice_note(
    input_path: str,
    loudnorm: bool = True,
    pitch: str = "normal"
) -> Tuple[bool, Optional[str], Optional[str]]:
    output_path = os.path.join(settings.TEMP_DIR, f"voice_{uuid.uuid4().hex[:8]}.ogg")
    
    filters = []
    if pitch == "deep":
        filters.append("asetrate=48000*0.94387,aresample=48000")
    if loudnorm:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    filter_str = ",".join(filters) if filters else "anull"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", filter_str,
        "-c:a", "libopus",
        "-b:a", "48k",
        "-ar", "48000",
        "-ac", "1",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(output_path):
            return True, output_path, None
        err_msg = stderr.decode()[-300:]
        return False, None, f"FFmpeg audio conversion failed: {err_msg}"
    except Exception as e:
        logger.error(f"Exception in convert_to_voice_note: {e}")
        return False, None, str(e)

def safe_remove(path: Optional[str]):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            logger.warning(f"Could not remove temp file {path}: {e}")
