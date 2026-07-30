"""오디오: 음성 / BGM / 효과음 믹싱 필터 그래프 생성 (덕킹, 페이드 포함)."""
from __future__ import annotations

from pathlib import Path

SAMPLE_RATE = 44100
AFORMAT = f"aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=stereo"


def _f(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_audio_graph(
    project: dict,
    total_duration: float,
    voice_path: Path | None,
    bgm_path: Path | None,
    sfx: list[tuple[Path, float, float]],
    first_input_index: int = 1,
) -> tuple[list[str], list[str], str]:
    """
    반환: (ffmpeg 입력 인자, 필터 그래프 조각 목록, 최종 오디오 라벨)

    sfx: [(경로, 시작초, 개별볼륨)]
    """
    cfg = project.get("audio", {})
    voice_volume = _f(cfg.get("voice_volume"), 1.0)
    bgm_volume = _f(cfg.get("bgm_volume"), 0.22)
    sfx_volume = _f(cfg.get("sfx_volume"), 0.7)
    ducking = bool(cfg.get("ducking", True))
    fade_in = max(0.0, _f(cfg.get("fade_in"), 0.3))
    fade_out = max(0.0, _f(cfg.get("fade_out"), 0.6))

    T = max(0.1, float(total_duration))
    inputs: list[str] = []
    filters: list[str] = []
    mix_labels: list[str] = []
    idx = first_input_index

    # 항상 무음 베이스를 넣어 오디오 스트림 존재를 보장한다.
    inputs += ["-f", "lavfi", "-t", f"{T:.3f}", "-i",
               f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"]
    filters.append(f"[{idx}:a]{AFORMAT}[abase]")
    mix_labels.append("[abase]")
    idx += 1

    sidechain_label = None
    if voice_path is not None:
        inputs += ["-i", str(voice_path)]
        filters.append(f"[{idx}:a]{AFORMAT}[voice0]")
        idx += 1
        if ducking and bgm_path is not None:
            # 사이드체인은 볼륨 조절 전 원본을 쓴다 (음성 볼륨을 낮춰도 덕킹은 유지).
            filters.append("[voice0]asplit=2[voicepre][voiceraw]")
            filters.append(f"[voicepre]volume={voice_volume:.3f}[voicemain]")
            filters.append(f"[voiceraw]apad=whole_dur={T:.3f},atrim=0:{T:.3f},asetpts=N/SR/TB[voicesc]")
            sidechain_label = "[voicesc]"
        else:
            filters.append(f"[voice0]volume={voice_volume:.3f}[voicemain]")
        mix_labels.append("[voicemain]")

    if bgm_path is not None:
        # -t 를 입력 옵션으로 걸어 무한 반복 입력이 영원히 디코딩되는 것을 막는다.
        inputs += ["-stream_loop", "-1", "-t", f"{T:.3f}", "-i", str(bgm_path)]
        filters.append(
            f"[{idx}:a]{AFORMAT},atrim=0:{T:.3f},asetpts=N/SR/TB,volume={bgm_volume:.3f}[bgm0]"
        )
        idx += 1
        if sidechain_label:
            threshold = _f(cfg.get("duck_threshold"), 0.045)
            ratio = min(20.0, max(1.0, _f(cfg.get("duck_ratio"), 9.0)))
            attack = min(2000.0, max(0.01, _f(cfg.get("duck_attack"), 15.0)))
            release = min(9000.0, max(0.01, _f(cfg.get("duck_release"), 380.0)))
            filters.append(
                f"[bgm0]{sidechain_label}sidechaincompress="
                f"threshold={threshold:.4f}:ratio={ratio:.2f}:attack={attack:.2f}:"
                f"release={release:.2f}:makeup=1:detection=rms[bgmmain]"
            )
        else:
            filters.append("[bgm0]anull[bgmmain]")
        mix_labels.append("[bgmmain]")

    for i, (path, start, volume) in enumerate(sfx):
        delay_ms = int(max(0.0, start) * 1000)
        inputs += ["-i", str(path)]
        gain = sfx_volume * _f(volume, 1.0)
        filters.append(
            f"[{idx}:a]{AFORMAT},volume={gain:.3f},adelay={delay_ms}|{delay_ms}[sfx{i}]"
        )
        idx += 1
        mix_labels.append(f"[sfx{i}]")

    if len(mix_labels) == 1:
        filters.append(f"{mix_labels[0]}anull[amixed]")
    else:
        filters.append(
            "".join(mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=longest:normalize=0:dropout_transition=0[amixed]"
        )

    chain = [f"atrim=0:{T:.3f}", "asetpts=N/SR/TB", f"apad=whole_dur={T:.3f}"]
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        chain.append(f"afade=t=out:st={max(0.0, T - fade_out):.3f}:d={fade_out:.3f}")
    chain.append(AFORMAT)
    filters.append("[amixed]" + ",".join(chain) + "[aout]")

    return inputs, filters, "[aout]"


def describe(project: dict, has_voice: bool, has_bgm: bool, sfx_count: int) -> str:
    cfg = project.get("audio", {})
    parts = []
    parts.append(f"음성 {'있음' if has_voice else '없음'} (볼륨 {_f(cfg.get('voice_volume'), 1.0):.2f})")
    parts.append(f"BGM {'있음' if has_bgm else '없음'} (볼륨 {_f(cfg.get('bgm_volume'), 0.22):.2f})")
    parts.append(f"효과음 {sfx_count}개")
    if cfg.get("ducking", True) and has_voice and has_bgm:
        parts.append("덕킹 켜짐")
    return " · ".join(parts)
