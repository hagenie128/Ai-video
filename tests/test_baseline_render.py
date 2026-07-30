"""STEP 0 회귀 테스트: 기존 렌더링 파이프라인이 정상 동작하는지 확인.

실행: python tests/test_baseline_render.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_manager as pm  # noqa: E402
import renderer  # noqa: E402
import timeline as tl  # noqa: E402
from tests.make_fixtures import make_audio, make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, media_info, require_ffmpeg  # noqa: E402

PROJECT_NAME = "테스트 프로젝트 baseline"
FIXTURES = TEMP_DIR / "fixtures"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def build_project() -> dict:
    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(PROJECT_NAME, "dark_warning")

    images = make_images(FIXTURES / "images", 9)
    v1 = make_video(FIXTURES / "videos" / "ai_clip_1.mp4", 4.0, "0x402020")
    v2 = make_video(FIXTURES / "videos" / "ai_clip_2.mp4", 5.0, "0x204020")
    voice = make_audio(FIXTURES / "voice.mp3", 12.0)
    bgm = make_audio(FIXTURES / "bgm.mp3", 30.0)

    for i, img in enumerate(images):
        rel = pm.save_upload(p, "images", img.name, img.read_bytes())
        cut = pm.new_cut("image", rel, img.name, 1.2, "slow_zoom_in" if i % 2 else "pan_left_to_right")
        cut["subtitle"] = f"테스트 자막 {i + 1}"
        p["cuts"].append(cut)

    for vid in (v1, v2):
        rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
        info = media_info(pm.abs_path(p, rel))
        cut = pm.new_cut("video", rel, vid.name, 1.5, "none")
        cut["source_duration"] = round(info["duration"], 2)
        cut["subtitle"] = "AI 영상 컷"
        p["cuts"].append(cut)

    rel = pm.save_upload(p, "voice", voice.name, voice.read_bytes())
    p["voice"] = {"file": rel, "duration": 12.0, "name": voice.name}
    rel = pm.save_upload(p, "bgm", bgm.name, bgm.read_bytes())
    p["bgm"] = {"file": rel, "name": bgm.name}
    pm.save_project(p)
    return p


def main() -> int:
    require_ffmpeg()
    print("STEP 0 baseline 회귀 테스트")

    p = build_project()
    check("프로젝트 생성/저장", pm.project_json_path(p).is_file())
    check("컷 11개 등록", len(p["cuts"]) == 11, f"{len(p['cuts'])}개")

    ok, msg = tl.fit_to_target(p)
    check("목표 길이 맞추기", ok, msg)

    subs = renderer.export_subtitles(p)
    check("SRT/ASS 생성", bool(subs), str(subs[0].name) if subs else "없음")

    print("  ... 미리보기 렌더링")
    preview = renderer.render(p, preview=True)
    check("미리보기 MP4 생성", preview.is_file())

    print("  ... 최종 렌더링")
    final = renderer.render(p, preview=False)
    check("최종 MP4 생성", final.is_file())

    info = media_info(final)
    check("해상도 1080x1920", (info["width"], info["height"]) == (1080, 1920), f"{info['width']}x{info['height']}")
    check("오디오 스트림 존재", info["has_audio"])
    expected = tl.total_duration(p)
    check("길이 오차 0.4초 이내", abs(info["duration"] - expected) < 0.4,
          f"실제 {info['duration']:.2f}s / 예상 {expected:.2f}s")

    reloaded = pm.load_project(p["name"])
    check("프로젝트 재불러오기", len(reloaded["cuts"]) == len(p["cuts"]))

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
