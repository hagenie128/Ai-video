"""자막: 대본 자동 분할, SRT / ASS 생성."""
from __future__ import annotations

import re
from pathlib import Path

from utils import format_ass_time, format_timestamp, hex_to_ass_color

ALIGN_MAP = {"left": 1, "center": 2, "right": 3}
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？…])\s+|\n+")


# ---------------------------------------------------------------- 텍스트 분할

def wrap_text(text: str, max_chars: int) -> str:
    """한 줄 최대 글자 수에 맞춰 줄바꿈."""
    max_chars = max(4, int(max_chars or 14))
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        while len(word) > max_chars:              # 아주 긴 단어는 강제로 자름
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def split_script(script: str, max_chars: int) -> list[str]:
    """대본을 자막 단위로 자동 분할."""
    max_chars = max(4, int(max_chars or 14))
    chunk_limit = max_chars * 2                   # 자막 1개 = 최대 2줄
    chunks: list[str] = []
    for sentence in SENTENCE_SPLIT.split(script or ""):
        sentence = re.sub(r"\s+", " ", sentence.strip())
        if not sentence:
            continue
        if len(sentence) <= chunk_limit:
            chunks.append(sentence)
            continue
        # 쉼표 우선, 그다음 공백 기준으로 다시 자름
        parts = [p.strip() for p in re.split(r"(?<=[,，·、])\s*", sentence) if p.strip()]
        buffer = ""
        for part in parts:
            if not buffer:
                buffer = part
            elif len(buffer) + 1 + len(part) <= chunk_limit:
                buffer = f"{buffer} {part}"
            else:
                chunks.append(buffer)
                buffer = part
        if buffer:
            for i in range(0, len(buffer), chunk_limit):
                chunks.append(buffer[i:i + chunk_limit])
    return chunks


# ---------------------------------------------------------------- 큐 생성

def build_cues(project: dict, timed: list[dict], total_video_duration: float) -> list[dict]:
    """[{'start','end','text'}] 반환. mode 에 따라 컷 기준 / 대본 기준."""
    cfg = project.get("subtitle", {})
    mode = cfg.get("mode", "cut")
    max_chars = cfg.get("max_chars", 14)
    cues: list[dict] = []

    if mode == "script":
        chunks = split_script(project.get("script", ""), max_chars)
        if not chunks or total_video_duration <= 0:
            return []
        weights = [max(1, len(c)) for c in chunks]
        total_weight = sum(weights)
        clock = 0.0
        for chunk, weight in zip(chunks, weights):
            span = total_video_duration * weight / total_weight
            cues.append({
                "start": round(clock, 3),
                "end": round(min(clock + span, total_video_duration), 3),
                "text": wrap_text(chunk, max_chars),
            })
            clock += span
    else:
        for item in timed:
            text = (item["cut"].get("subtitle") or "").strip()
            if not text:
                continue
            cues.append({
                "start": item["start"],
                "end": item["end"],
                "text": wrap_text(text, max_chars),
            })

    return [c for c in cues if c["text"] and c["end"] > c["start"] + 0.05]


# ---------------------------------------------------------------- 파일 출력

def write_srt(cues: list[dict], path: Path) -> Path:
    lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{format_timestamp(cue['start'])} --> {format_timestamp(cue['end'])}")
        lines.append(cue["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_ass(project: dict, cues: list[dict]) -> str:
    cfg = project.get("subtitle", {})
    width = int(project.get("width", 1080))
    height = int(project.get("height", 1920))

    font = cfg.get("font") or "Malgun Gothic"
    font_size = int(cfg.get("font_size", 74))
    bold = -1 if cfg.get("bold", True) else 0
    primary = hex_to_ass_color(cfg.get("primary_color", "#FFFFFF"))
    outline_color = hex_to_ass_color(cfg.get("outline_color", "#000000"))
    outline = float(cfg.get("outline", 6))
    shadow = float(cfg.get("shadow", 2))
    margin_v = int(cfg.get("margin_v", 300))
    align = ALIGN_MAP.get(cfg.get("align", "center"), 2)

    header = f"""[Script Info]
Title: {project.get('name', 'shorts')}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{primary},{primary},{outline_color},&H80000000,{bold},0,0,0,100,100,0,0,1,{outline:g},{shadow:g},{align},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for cue in cues:
        text = cue["text"].replace("\\", "\\\\").replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{format_ass_time(cue['start'])},{format_ass_time(cue['end'])},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


def write_ass(project: dict, cues: list[dict], path: Path) -> Path:
    path.write_text(build_ass(project, cues), encoding="utf-8")
    return path
