"""STEP 1 테스트: 설정/비밀값, LLM JSON 파싱, 규칙 기반 대본, Edge TTS.

실행: python tests/test_step1.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_provider  # noqa: E402
import config  # noqa: E402
import project_manager as pm  # noqa: E402
import script_generator as sg  # noqa: E402
import tts_service  # noqa: E402
from utils import PROJECTS_DIR  # noqa: E402

failures: list[str] = []
skipped: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def skip(label: str, reason: str) -> None:
    print(f"  [SKIP] {label} — {reason}")
    skipped.append(label)


BRIEF = {
    **sg.empty_brief(),
    "project_name": "테스트 가죽지갑",
    "category": "가죽 지갑",
    "product_name": "핸드메이드 반지갑",
    "description": "이탈리아산 베지터블 가죽으로 만든 반지갑입니다. 실은 폴리에스터 30수를 씁니다.",
    "benefits": "손바느질 마감\n금속 부속 없음\n두께 12mm",
    "facts": "가죽 두께 1.4mm\n제작 기간 5일",
    "target": "30대 남성",
    "mood": "차분하고 담백한",
    "forbidden": "최고급\n무조건",
    "brand_visible": False,
    "brand_name": "테스트브랜드",
    "target_length": 30,
    "content_type": "consumer_warning",
}


def test_config() -> None:
    print("[1] config / 비밀값")
    backup = config.ENV_PATH.read_bytes() if config.ENV_PATH.is_file() else None
    try:
        config.set_secret("anthropic", "sk-test-1234567890abcdef")
        check("환경변수 저장", config.get_secret("anthropic") == "sk-test-1234567890abcdef")
        check("마스킹", config.mask(config.get_secret("anthropic")) == "sk-t**********cdef",
              config.mask(config.get_secret("anthropic")))
        config.set_secret("anthropic", "")
        check("비밀값 삭제", config.get_secret("anthropic") == "")

        settings = config.load_settings()
        settings["llm"]["provider"] = "anthropic"
        settings["llm"]["api_key"] = "LEAK-should-be-stripped"
        config.save_settings(settings)
        saved = config.SETTINGS_PATH.read_text(encoding="utf-8")
        check("설정 JSON에 키 저장 안 됨", "LEAK" not in saved)
        check("설정 값 유지", config.load_settings()["llm"]["provider"] == "anthropic")
        check("기본 TTS 는 Edge", config.DEFAULT_SETTINGS["tts"]["provider"] == "edge")
    finally:
        if backup is not None:
            config.ENV_PATH.write_bytes(backup)
        elif config.ENV_PATH.is_file():
            config.ENV_PATH.unlink()
        config.SETTINGS_PATH.unlink(missing_ok=True)


def test_json_extract() -> None:
    print("[2] LLM 응답 JSON 파싱")
    cases = [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 2}\n```', {"a": 2}),
        ('설명입니다.\n{"a": 3, "b": {"c": "}"}}\n끝', {"a": 3, "b": {"c": "}"}}),
        ("JSON 없음", None),
    ]
    for raw, expected in cases:
        check(f"파싱: {raw[:24]!r}", ai_provider.extract_json(raw) == expected)
    check("LLM 미설정 감지", ai_provider.available() is False or config.has_secret("anthropic"))


def test_script() -> None:
    print("[3] 규칙 기반 대본 생성")
    script = sg.generate_rule_based(BRIEF)
    scenes = script["scenes"]
    check("장면 생성", len(scenes) >= 6, f"{len(scenes)}개")
    check("첫 장면 hook", scenes[0]["role"] == "hook")
    check("마지막 장면 ending", scenes[-1]["role"] == "ending")
    check("첫 3초 최소 3컷", sg.first_three_second_cuts(scenes) >= 3,
          f"{sg.first_three_second_cuts(scenes)}컷")
    check("목표 길이 근접", abs(script["estimated_duration"] - 30) < 1.0,
          f"{script['estimated_duration']}초")
    check("자막 두 줄 이하", all(s["subtitle"].count("\n") <= 1 for s in scenes))
    check("자막 한 줄 길이 제한", all(
        all(len(line) <= 20 for line in s["subtitle"].split("\n")) for s in scenes))
    check("visual_type 유효", all(s["visual_type"] in sg.VISUAL_TYPES for s in scenes))
    check("전체 대본 채워짐", len(script["full_script"]) > 20)

    forbidden = sg.check_forbidden(script, BRIEF)
    check("금지 표현 제거됨", not any("최고급" in h or "무조건" in h for h in forbidden),
          str(forbidden))

    facts = sg.check_facts(script, BRIEF)
    bad = [w for w in facts if "없는 수치" in w]
    check("근거 없는 수치 없음", not bad, str(bad))

    shorter, msg = sg.resize(dict(script, scenes=[dict(s) for s in scenes]), BRIEF, "shorter")
    check("길이 줄이기", len(shorter["scenes"]) < len(scenes), msg)
    longer, msg = sg.resize(dict(script, scenes=[dict(s) for s in scenes]), BRIEF, "longer")
    check("길이 늘리기", len(longer["scenes"]) > len(scenes), msg)

    hooked, msg = sg.regenerate_hook(dict(script, scenes=[dict(s) for s in scenes]), BRIEF, variant=1)
    check("후킹 재생성", hooked["scenes"][0]["voiceover"] != scenes[0]["voiceover"], msg)

    rebuilt, msg = sg.rebuild_from_full_script(
        dict(script, scenes=[dict(s) for s in scenes]), BRIEF,
        "첫 문장입니다. 두 번째 문장. 세 번째 문장이에요.")
    check("전체 대본 재배분", rebuilt["scenes"][0]["voiceover"].startswith("첫 문장"), msg)

    # 자료가 거의 없어도 죽지 않아야 한다
    thin = {**sg.empty_brief(), "product_name": "무명 제품", "target_length": 15}
    thin_script = sg.generate_rule_based(thin)
    check("빈 자료에도 생성", len(thin_script["scenes"]) >= 5, f"{len(thin_script['scenes'])}개")

    for ctype in sg.CONTENT_TYPES:
        result = sg.generate_rule_based({**BRIEF, "content_type": ctype})
        if not result["scenes"]:
            check(f"콘텐츠 유형 {ctype}", False)
            return
    check("모든 콘텐츠 유형 생성", True, f"{len(sg.CONTENT_TYPES)}종")


def _stub_synth(text: str, out_path: Path, settings: dict | None = None) -> Path:
    """네트워크 없이 합성 이후 로직(이어붙이기·타이밍·자막)을 검증하기 위한 대체 합성기."""
    import subprocess

    from utils import find_ffmpeg

    seconds = max(0.6, len(text) / 5.6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=300:duration={seconds:.3f}",
         "-c:a", "libmp3lame", str(out_path)],
        check=True,
    )
    return out_path


def test_tts() -> None:
    print("[4] TTS (Edge TTS 우선, 실패 시 스텁으로 이후 로직 검증)")
    check("edge-tts 설치 감지", tts_service.edge_available())

    name = "테스트 TTS 프로젝트"
    target = PROJECTS_DIR / pm.safe_filename(name)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(name, "dark_warning")

    settings = {**config.DEFAULT_SETTINGS["tts"], "provider": "edge", "voice": "ko-KR-SunHiNeural"}
    script = sg.generate_rule_based(BRIEF)
    scenes = script["scenes"][:4]

    try:
        result = tts_service.synthesize_scenes(p, scenes, settings)
        print("  (실제 Edge TTS 합성 성공)")
    except (tts_service.TTSError, tts_service.TTSUnavailable) as exc:
        skip("실제 Edge TTS 합성", f"이 환경의 네트워크 정책: {str(exc)[:90]}")
        real = tts_service.synthesize_text
        tts_service.synthesize_text = _stub_synth       # type: ignore[assignment]
        try:
            result = tts_service.synthesize_scenes(p, scenes, settings)
        finally:
            tts_service.synthesize_text = real          # type: ignore[assignment]

    check("음성 파일 생성", pm.abs_path(p, result["file"]).is_file())
    check("길이 측정", result["duration"] > 1.0, f"{result['duration']:.2f}초")
    check("장면 수 일치", len(result["segments"]) == len(scenes))
    check("세그먼트 시간 증가", all(
        result["segments"][i]["end"] <= result["segments"][i + 1]["start"] + 0.001
        for i in range(len(result["segments"]) - 1)))
    check("마지막 세그먼트가 전체 길이 안", result["segments"][-1]["end"] <= result["duration"] + 0.3,
          f"{result['segments'][-1]['end']:.2f} / {result['duration']:.2f}")

    cues = tts_service.cues_from_segments(result["segments"], 14)
    check("자막 큐 생성", len(cues) >= len(scenes), f"{len(cues)}개")
    check("자막 큐 시간 유효", all(c["end"] > c["start"] for c in cues))
    print("  " + tts_service.length_advice(result["duration"], 30))


def main() -> int:
    print("STEP 1 테스트\n")
    test_config()
    test_json_extract()
    test_script()
    test_tts()
    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print(f"모두 통과 (건너뜀 {len(skipped)}건: {skipped})" if skipped else "모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
