"""공통 유틸리티: 경로, FFmpeg 실행, 미디어 정보 조회."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Iterable, Sequence

APP_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = APP_DIR / "projects"
OUTPUTS_DIR = APP_DIR / "outputs"
TEMP_DIR = APP_DIR / "temp"
PRESETS_DIR = APP_DIR / "presets"

for _d in (PROJECTS_DIR, OUTPUTS_DIR, TEMP_DIR, PRESETS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    """FFmpeg 실행 실패."""

    def __init__(self, message: str, cmd: Sequence[str], stderr: str, returncode: int):
        super().__init__(message)
        self.cmd = list(cmd)
        self.stderr = stderr
        self.returncode = returncode

    def pretty(self) -> str:
        return friendly_ffmpeg_error(self.stderr)

    def command_line(self) -> str:
        return " ".join(_quote(a) for a in self.cmd)


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or "\\" in arg) else arg


# ---------------------------------------------------------------- 실행 파일 탐색

_EXTRA_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
    Path("C:/ffmpeg/bin"),
    Path("C:/Program Files/ffmpeg/bin"),
    Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims",
    Path("C:/ProgramData/chocolatey/bin"),
    APP_DIR / "bin",
]


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _EXTRA_DIRS:
        try:
            candidate = d / exe
        except (TypeError, ValueError):
            continue
        if candidate.is_file():
            return str(candidate)
    return None


def find_ffmpeg() -> str | None:
    return _find_binary("ffmpeg")


def find_ffprobe() -> str | None:
    return _find_binary("ffprobe")


FFMPEG_INSTALL_HELP = """FFmpeg를 찾을 수 없습니다.

설치 방법 (아래 중 하나):
  1) PowerShell에서:  winget install --id Gyan.FFmpeg -e
  2) https://www.gyan.dev/ffmpeg/builds/ 에서 full build zip 다운로드 →
     C:\\ffmpeg 에 압축 해제 → C:\\ffmpeg\\bin 을 PATH 환경변수에 추가
  3) 또는 ffmpeg.exe / ffprobe.exe 를 이 앱 폴더의 bin\\ 안에 복사

설치 후 터미널(및 이 앱)을 다시 실행하세요."""


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise RuntimeError(FFMPEG_INSTALL_HELP)
    return ffmpeg, ffprobe


# ---------------------------------------------------------------- FFmpeg 실행

def run_ffmpeg(
    args: Sequence[str],
    cwd: Path | None = None,
    total_duration: float | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> str:
    """ffmpeg 실행. args 는 ffmpeg 실행 파일 뒤에 붙는 인자 목록."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(FFMPEG_INSTALL_HELP)

    # -nostats: 진행 상태는 -progress 로만 받는다.
    # 진행 상태는 파이프가 아니라 작업 폴더의 파일로 받는다.
    # (stdout 파이프로 받으면 인코딩 도중 교착이 생길 수 있다.)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y"]
    use_progress = bool(progress_cb and total_duration and cwd)
    progress_file = Path(cwd) / "_progress.txt" if use_progress else None
    if progress_file is not None:
        progress_file.write_text("", encoding="utf-8")
        cmd += ["-progress", progress_file.name, "-stats_period", "0.3"]
    cmd += [str(a) for a in args]

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )

    # stderr 는 별도 스레드로 계속 비워 파이프 교착을 막는다.
    stderr_chunks: list[str] = []

    def _drain() -> None:
        if proc.stderr is not None:
            for line in proc.stderr:
                stderr_chunks.append(line)

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    if progress_file is not None:
        last = 0.0
        while proc.poll() is None:
            time.sleep(0.3)
            seconds = _read_progress(progress_file)
            if seconds is None:
                continue
            fraction = min(1.0, seconds / max(total_duration, 0.01))
            if fraction > last:                     # 진행률은 되돌아가지 않게 한다
                last = fraction
                progress_cb(fraction)

    proc.wait()
    drainer.join(timeout=5)
    if progress_file is not None:
        progress_file.unlink(missing_ok=True)
    stderr = "".join(stderr_chunks)

    if proc.returncode != 0:
        raise FFmpegError(
            f"FFmpeg 실행 실패 (exit {proc.returncode})", cmd, stderr, proc.returncode
        )
    if progress_cb:
        progress_cb(1.0)
    return stderr


def _read_progress(path: Path) -> float | None:
    """ffmpeg -progress 파일에서 마지막 진행 시간(초)을 읽는다."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # out_time_ms 는 실제로 마이크로초 단위라 혼동을 피하려고 out_time_us 만 쓴다.
    for line in reversed(text.splitlines()):
        if line.startswith("out_time_us="):
            raw = line.split("=", 1)[1].strip()
            if raw.lstrip("-").isdigit():
                value = int(raw) / 1_000_000
                return value if value >= 0 else None
    return None


def friendly_ffmpeg_error(stderr: str) -> str:
    """FFmpeg stderr 를 읽기 쉬운 한국어 안내로 요약."""
    s = stderr or ""
    low = s.lower()
    hints: list[str] = []
    if "no such file or directory" in low:
        hints.append("· 입력 파일 경로를 찾을 수 없습니다. 파일이 삭제되었는지 확인하세요.")
    if "invalid argument" in low and "filter" in low:
        hints.append("· 필터 설정값이 잘못되었습니다. 컷 길이나 효과 설정을 확인하세요.")
    if "unable to find a suitable output format" in low:
        hints.append("· 출력 파일 확장자가 잘못되었습니다.")
    if "does not contain any stream" in low or "invalid data found" in low:
        hints.append("· 손상되었거나 지원하지 않는 미디어 파일입니다.")
    if "height not divisible by 2" in low or "width not divisible by 2" in low:
        hints.append("· 해상도가 짝수가 아닙니다.")
    if "fontconfig" in low or "ass" in low and "error" in low:
        hints.append("· 자막(ASS) 처리 오류입니다. 폰트 이름을 '맑은 고딕'/'Malgun Gothic' 으로 바꿔보세요.")
    if "unknown encoder" in low:
        hints.append("· 이 FFmpeg 빌드에 필요한 인코더가 없습니다. full build 를 설치하세요.")
    if "permission denied" in low:
        hints.append("· 파일이 다른 프로그램에서 열려 있습니다. 닫고 다시 시도하세요.")
    if not hints:
        hints.append("· 아래 원본 오류 메시지를 확인하세요.")

    tail = "\n".join(s.strip().splitlines()[-25:])
    return "\n".join(hints) + "\n\n--- FFmpeg 원본 오류 ---\n" + tail


# ---------------------------------------------------------------- 미디어 정보

def probe(path: Path) -> dict:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RuntimeError(FFMPEG_INSTALL_HELP)
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise FFmpegError("ffprobe 실패", cmd, proc.stderr, proc.returncode)
    return json.loads(proc.stdout or "{}")


def media_info(path: Path) -> dict:
    """{'duration': float, 'has_video': bool, 'has_audio': bool, 'width': int, 'height': int}"""
    info = probe(path)
    duration = 0.0
    try:
        duration = float(info.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    result = {"duration": duration, "has_video": False, "has_audio": False, "width": 0, "height": 0}
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("disposition", {}).get("attached_pic", 0) == 0:
            result["has_video"] = True
            result["width"] = int(stream.get("width") or 0)
            result["height"] = int(stream.get("height") or 0)
            if not duration:
                try:
                    result["duration"] = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif stream.get("codec_type") == "audio":
            result["has_audio"] = True
    return result


def audio_duration(path: Path) -> float:
    return round(media_info(path)["duration"], 3)


# ---------------------------------------------------------------- 파일명 / 경로

_INVALID = r'<>:"/\\|?*'


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name).strip()
    name = "".join("_" if (c in _INVALID or ord(c) < 32) else c for c in name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "untitled"


def unique_path(path: Path) -> Path:
    """같은 이름이 있으면 _1, _2 를 붙여 충돌을 피한다."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def cleanup_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def cleanup_old_temp(max_age_seconds: float = 1800) -> None:
    """중단된 작업이 남긴 오래된 임시 폴더 정리 (진행 중인 작업은 건드리지 않는다)."""
    now = time.time()
    try:
        entries = [p for p in TEMP_DIR.iterdir() if p.is_dir()]
    except OSError:
        return
    for d in entries:
        try:
            if now - d.stat().st_mtime > max_age_seconds:
                cleanup_dir(d)
        except OSError:
            continue


def hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR"""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def format_timestamp(seconds: float, comma: bool = True) -> str:
    """SRT 용 00:00:00,000"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def format_ass_time(seconds: float) -> str:
    """ASS 용 0:00:00.00"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"
