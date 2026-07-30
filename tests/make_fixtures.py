"""테스트용 더미 사진/영상/음성 생성 (ffmpeg + Pillow)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import find_ffmpeg  # noqa: E402

COLORS = [
    (28, 28, 34), (120, 40, 40), (40, 90, 120), (150, 130, 60), (60, 110, 70),
    (100, 60, 130), (180, 90, 40), (45, 45, 55), (200, 190, 175),
]


def make_images(out_dir: Path, count: int = 9) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        w, h = (1200, 1600) if i % 3 else (1600, 1200)
        img = Image.new("RGB", (w, h), COLORS[i % len(COLORS)])
        d = ImageDraw.Draw(img)
        d.rectangle([w * 0.2, h * 0.25, w * 0.8, h * 0.75], outline=(255, 255, 255), width=12)
        d.text((w * 0.25, h * 0.45), f"TEST {i + 1}", fill=(255, 255, 255))
        path = out_dir / f"photo_{i + 1:02d}.jpg"
        img.save(path, quality=90)
        paths.append(path)
    return paths


def make_video(path: Path, seconds: float = 4.0, color: str = "0x203040") -> Path:
    ffmpeg = find_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc2=s=1080x1920:r=30:d={seconds}",
         "-vf", f"drawbox=c={color}@0.35:t=fill",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)],
        check=True,
    )
    return path


def make_audio(path: Path, seconds: float = 12.0) -> Path:
    ffmpeg = find_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=320:duration={seconds}",
         "-af", "tremolo=f=3:d=0.7", "-c:a", "libmp3lame", str(path)],
        check=True,
    )
    return path


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "temp/fixtures")
    make_images(root / "images")
    make_video(root / "videos" / "ai_clip_1.mp4", 4.0, "0x402020")
    make_video(root / "videos" / "ai_clip_2.mp4", 5.0, "0x204020")
    make_audio(root / "voice.mp3", 12.0)
    make_audio(root / "bgm.mp3", 30.0)
    print("fixtures:", root)
