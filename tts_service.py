"""한국어 TTS: Edge TTS(기본 무료) / ElevenLabs / OpenAI TTS / 직접 업로드.

장면 단위로 각각 합성한 뒤 이어 붙인다 → 장면별 실제 발화 시각을 정확히 알 수 있고
자막 싱크가 어긋나지 않는다. 실패하면 항상 '직접 업로드'로 대체 가능하다.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import config
import project_manager as pm
from utils import TEMP_DIR, FFmpegError, audio_duration, find_ffmpeg, run_ffmpeg, unique_path

PROVIDERS = ["edge", "elevenlabs", "openai", "upload"]
PROVIDER_LABELS = {
    "edge": "Edge TTS (무료, 설치만 필요)",
    "elevenlabs": "ElevenLabs (API 키 필요)",
    "openai": "OpenAI TTS (API 키 필요)",
    "upload": "직접 음성 업로드",
}

# 한국어 기본 음성 프리셋
VOICE_STYLES = {
    "calm_female": {
        "label": "차분한 여성",
        "edge": "ko-KR-SunHiNeural", "openai": "nova", "rate": 0.0, "pitch": 0.0,
    },
    "calm_male": {
        "label": "차분한 남성",
        "edge": "ko-KR-InJoonNeural", "openai": "onyx", "rate": 0.0, "pitch": 0.0,
    },
    "fast_info": {
        "label": "빠른 정보형",
        "edge": "ko-KR-SunHiNeural", "openai": "alloy", "rate": 0.14, "pitch": 0.02,
    },
    "dry_warning": {
        "label": "낮고 건조한 경고형",
        "edge": "ko-KR-InJoonNeural", "openai": "onyx", "rate": -0.05, "pitch": -0.06,
    },
}

EDGE_KOREAN_VOICES = [
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "ko-KR-HyunsuMultilingualNeural",
]
OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]

EDGE_INSTALL_HELP = (
    "Edge TTS 가 설치되지 않았습니다.\n\n"
    "명령 프롬프트에서:  pip install edge-tts\n"
    "(run.bat 을 다시 실행하면 자동 설치됩니다)"
)

GAP_SECONDS = 0.12          # 장면 사이 자연스러운 호흡
MIN_TEXT_LEN = 1


class TTSError(RuntimeError):
    """TTS 합성 실패."""


class TTSUnavailable(RuntimeError):
    """공급자 미설치/미설정 → 수동 업로드로 대체해야 하는 상황."""


@dataclass
class Segment:
    scene_id: str
    text: str
    path: Path
    duration: float


# ---------------------------------------------------------------- 설정 헬퍼

def current() -> dict:
    return config.load_settings()["tts"]


def resolve_voice(settings: dict | None = None) -> tuple[str, str, float, float]:
    """(provider, voice, rate, pitch) — rate/pitch 는 -1.0~1.0 상대값."""
    cfg = settings or current()
    provider = cfg.get("provider", "edge")
    style = VOICE_STYLES.get(cfg.get("style", "calm_female"), VOICE_STYLES["calm_female"])
    voice = cfg.get("voice") or style.get(provider if provider in ("edge", "openai") else "edge", "")
    rate = float(cfg.get("rate", 1.0)) - 1.0 + float(style.get("rate", 0.0))
    pitch = float(cfg.get("pitch", 0.0)) + float(style.get("pitch", 0.0))
    return provider, voice, max(-0.5, min(1.0, rate)), max(-0.5, min(0.5, pitch))


def status_text() -> str:
    cfg = current()
    provider = cfg.get("provider", "edge")
    label = PROVIDER_LABELS.get(provider, provider)
    if provider == "edge":
        return f"{label} · {'설치됨' if edge_available() else '미설치'} · 음성 {cfg.get('voice')}"
    if provider == "elevenlabs":
        return f"{label} · 키 {'있음' if config.has_secret('elevenlabs') else '없음'}"
    if provider == "openai":
        return f"{label} · 키 {'있음' if config.has_secret('openai') else '없음'}"
    return label


# ---------------------------------------------------------------- Edge TTS

def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _pct(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}{abs(int(round(value * 100)))}%"


def _hz(value: float) -> str:
    hz = int(round(value * 50))
    return f"{'+' if hz >= 0 else '-'}{abs(hz)}Hz"


def _edge_synth(text: str, voice: str, rate: float, pitch: float, volume: float, out: Path) -> None:
    try:
        import edge_tts
    except Exception as exc:  # noqa: BLE001
        raise TTSUnavailable(EDGE_INSTALL_HELP) from exc

    async def run() -> bytes:
        communicate = edge_tts.Communicate(
            text, voice or "ko-KR-SunHiNeural",
            rate=_pct(rate), pitch=_hz(pitch), volume=_pct(volume - 1.0),
        )
        chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.extend(chunk["data"])
        return bytes(chunks)

    try:
        data = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - 네트워크/서비스 오류를 사용자에게 그대로 전달
        raise TTSError(f"Edge TTS 합성 실패: {exc}") from exc
    if not data:
        raise TTSError("Edge TTS 가 빈 음성을 반환했습니다.")
    out.write_bytes(data)


def edge_voice_list() -> list[str]:
    """실제 사용 가능한 한국어 음성 목록 (실패하면 기본 목록)."""
    try:
        import edge_tts

        async def run():
            return await edge_tts.list_voices()

        voices = asyncio.run(run())
        korean = sorted({v["ShortName"] for v in voices if str(v.get("Locale", "")).startswith("ko-")})
        return korean or EDGE_KOREAN_VOICES
    except Exception:  # noqa: BLE001
        return EDGE_KOREAN_VOICES


# ---------------------------------------------------------------- ElevenLabs

def _elevenlabs_synth(text: str, voice: str, rate: float, out: Path) -> None:
    key = config.get_secret("elevenlabs")
    if not key:
        raise TTSUnavailable("ElevenLabs API 키가 없습니다.")
    voice_id = voice or "21m00Tcm4TlvDq8ikWAM"
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.75,
            "style": max(0.0, min(1.0, float(current().get("emotion", 0.5)))),
            "speed": max(0.7, min(1.2, 1.0 + rate)),
        },
    }
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps(payload).encode("utf-8"), method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("xi-api-key", key)
    req.add_header("Accept", "audio/mpeg")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise TTSError(f"ElevenLabs 오류 {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TTSError(f"ElevenLabs 네트워크 오류: {exc.reason}") from exc
    if not data:
        raise TTSError("ElevenLabs 가 빈 음성을 반환했습니다.")
    out.write_bytes(data)


def elevenlabs_voices() -> list[tuple[str, str]]:
    key = config.get_secret("elevenlabs")
    if not key:
        return []
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices")
    req.add_header("xi-api-key", key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except Exception:  # noqa: BLE001
        return []
    return [(v.get("voice_id", ""), v.get("name", "")) for v in data.get("voices", []) if v.get("voice_id")]


# ---------------------------------------------------------------- OpenAI TTS

def _openai_synth(text: str, voice: str, rate: float, out: Path) -> None:
    key = config.get_secret("openai")
    if not key:
        raise TTSUnavailable("OpenAI API 키가 없습니다.")
    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": voice if voice in OPENAI_VOICES else "nova",
        "input": text,
        "response_format": "mp3",
        "speed": max(0.5, min(2.0, 1.0 + rate)),
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"), method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise TTSError(f"OpenAI TTS 오류 {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TTSError(f"OpenAI TTS 네트워크 오류: {exc.reason}") from exc
    if not data:
        raise TTSError("OpenAI TTS 가 빈 음성을 반환했습니다.")
    out.write_bytes(data)


# ---------------------------------------------------------------- 합성 진입점

def synthesize_text(text: str, out_path: Path, settings: dict | None = None) -> Path:
    """단일 텍스트 → 음성 파일."""
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if len(clean) < MIN_TEXT_LEN:
        raise TTSError("합성할 텍스트가 없습니다.")
    cfg = settings or current()
    provider, voice, rate, pitch = resolve_voice(cfg)
    volume = float(cfg.get("volume", 1.0))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "edge":
        _edge_synth(clean, voice, rate, pitch, volume, out_path)
    elif provider == "elevenlabs":
        _elevenlabs_synth(clean, voice, rate, out_path)
    elif provider == "openai":
        _openai_synth(clean, voice, rate, out_path)
    else:
        raise TTSUnavailable("현재 TTS 공급자는 '직접 업로드'입니다. 음성 파일을 업로드하세요.")
    return out_path


def test_connection() -> tuple[bool, str]:
    provider, voice, _, _ = resolve_voice()
    if provider == "upload":
        return False, "공급자가 '직접 업로드'입니다. 합성 테스트는 필요 없습니다."
    work = TEMP_DIR / "tts_test"
    work.mkdir(parents=True, exist_ok=True)
    out = work / "test.mp3"
    try:
        synthesize_text("테스트 음성입니다. 잘 들리나요.", out)
    except (TTSError, TTSUnavailable) as exc:
        return False, str(exc)
    try:
        seconds = audio_duration(out)
    except Exception:  # noqa: BLE001 - ffprobe 없이도 파일 생성 자체는 성공으로 본다
        seconds = 0.0
    return True, f"합성 성공 · {PROVIDER_LABELS.get(provider)} · {voice} · {seconds:.2f}초 · {out}"


# ---------------------------------------------------------------- 장면 단위 합성

def synthesize_scenes(
    project: dict,
    scenes: list[dict],
    settings: dict | None = None,
    progress_cb=None,
) -> dict:
    """장면별로 합성 → 하나로 이어 붙임.

    반환: {"file": 상대경로, "duration": float, "segments": [...], "provider": str, "gap": float}
    """
    if find_ffmpeg() is None:
        raise TTSError("FFmpeg 가 없어 음성을 이어 붙일 수 없습니다.")
    usable = [s for s in scenes if re.sub(r"\s+", "", s.get("voiceover") or "")]
    if not usable:
        raise TTSError("대본에 나레이션 문장이 없습니다.")

    cfg = settings or current()
    provider = cfg.get("provider", "edge")
    if provider == "upload":
        raise TTSUnavailable("TTS 공급자가 '직접 업로드'입니다. 음성 파일을 업로드하세요.")

    work = TEMP_DIR / f"tts_{pm.safe_filename(project['name'])}"
    if work.exists():
        for old in work.glob("*"):
            old.unlink(missing_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    segments: list[Segment] = []
    for i, scene in enumerate(usable):
        if progress_cb:
            progress_cb(i / len(usable), f"음성 합성 {i + 1}/{len(usable)}")
        part = work / f"seg_{i:03d}.mp3"
        synthesize_text(scene["voiceover"], part, cfg)
        try:
            duration = audio_duration(part)
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"음성 길이 측정 실패: {exc}") from exc
        if duration <= 0:
            raise TTSError(f"{scene.get('scene_id')} 음성 길이가 0입니다.")
        segments.append(Segment(scene.get("scene_id", f"S{i + 1:02d}"), scene["voiceover"], part, duration))

    if progress_cb:
        progress_cb(0.9, "음성 이어 붙이기")

    voice_dir = pm.ensure_dirs(project) / "media" / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_path(voice_dir / "voice_auto.mp3")
    _concat(segments, GAP_SECONDS, out_path, work)

    total = audio_duration(out_path)
    # 인코딩 과정에서 생기는 미세 오차를 보정해 자막이 음성 밖으로 나가지 않게 한다
    planned = sum(s.duration for s in segments) + GAP_SECONDS * max(0, len(segments) - 1)
    scale = (total / planned) if planned > 0 and total > 0 else 1.0

    timeline: list[dict] = []
    clock = 0.0
    for seg in segments:
        length = seg.duration * scale
        timeline.append({
            "scene_id": seg.scene_id,
            "text": seg.text,
            "start": round(clock, 3),
            "end": round(min(clock + length, total), 3),
            "duration": round(length, 3),
        })
        clock += length + GAP_SECONDS * scale

    for part in work.glob("seg_*.mp3"):
        part.unlink(missing_ok=True)

    return {
        "file": out_path.relative_to(pm.project_dir(project)).as_posix(),
        "name": out_path.name,
        "duration": round(total, 3),
        "segments": timeline,
        "provider": provider,
        "voice": cfg.get("voice", ""),
        "gap": GAP_SECONDS,
    }


def _concat(segments: list[Segment], gap: float, out_path: Path, work: Path) -> None:
    """장면 음성 사이에 gap 만큼 무음을 넣어 하나로 합친다."""
    args: list[str] = []
    for seg in segments:
        args += ["-i", str(seg.path)]
    filters = []
    labels = []
    for i, _ in enumerate(segments):
        pad = f",apad=pad_dur={gap:.3f}" if i < len(segments) - 1 else ""
        filters.append(f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{pad}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append("".join(labels) + f"concat=n={len(segments)}:v=0:a=1[aout]")
    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[aout]", "-c:a", "libmp3lame", "-q:a", "3", str(out_path),
    ]
    try:
        run_ffmpeg(args, cwd=work)
    except FFmpegError as exc:
        raise TTSError(f"음성 이어 붙이기 실패: {exc.pretty()}") from exc


def length_advice(tts_duration: float, target_seconds: float | None) -> str:
    """목표 길이와 실제 음성 길이 차이에 대한 안내."""
    if not target_seconds or target_seconds <= 0:
        return f"음성 길이 {tts_duration:.2f}초 (목표 길이 미지정 → 이 길이를 본편 길이로 사용합니다)."
    diff = tts_duration - float(target_seconds)
    if abs(diff) <= 1.5:
        return f"음성 {tts_duration:.2f}초 · 목표 {target_seconds:.0f}초 — 차이 {diff:+.2f}초로 적절합니다."
    if diff > 0:
        speed = min(1.25, tts_duration / float(target_seconds))
        return (f"음성이 목표보다 {diff:.1f}초 깁니다. '길이 줄이기'로 장면을 줄이거나 "
                f"속도를 약 {speed:.2f}배로 올리세요.")
    speed = max(0.85, tts_duration / float(target_seconds))
    return (f"음성이 목표보다 {abs(diff):.1f}초 짧습니다. '길이 늘리기'로 장면을 추가하거나 "
            f"속도를 약 {speed:.2f}배로 낮추세요.")


def cues_from_segments(segments: list[dict], max_chars: int = 14) -> list[dict]:
    """TTS 세그먼트 → 자막 큐. 문장 단위로 나누고 글자 수 비율로 시간 배분."""
    import script_generator as sg

    cues: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = float(seg.get("start", 0)), float(seg.get("end", 0))
        span = max(0.2, end - start)
        parts = [p for p in re.split(r"(?<=[.!?…])\s+|(?<=[,·])\s+", text) if p.strip()]
        if len(parts) <= 1:
            cues.append({"start": round(start, 3), "end": round(end, 3),
                         "text": sg.make_subtitle(text, max_chars)})
            continue
        weights = [max(1, len(re.sub(r"\s+", "", p))) for p in parts]
        total = sum(weights)
        clock = start
        for part, weight in zip(parts, weights):
            length = span * weight / total
            cues.append({
                "start": round(clock, 3),
                "end": round(min(clock + length, end), 3),
                "text": sg.make_subtitle(part, max_chars),
            })
            clock += length
    return [c for c in cues if c["text"] and c["end"] > c["start"] + 0.05]
