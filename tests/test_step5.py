"""STEP 5 테스트: 품질 검사(렌더 전/후), 월간 통계.

실행: python tests/test_step5.py
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import higgsfield_service as hf  # noqa: E402
import media_analyzer as ma  # noqa: E402
import project_manager as pm  # noqa: E402
import quality_checker as qc  # noqa: E402
import renderer  # noqa: E402
import scene_planner as sp  # noqa: E402
import script_generator as sg  # noqa: E402
import timeline as tl  # noqa: E402
from tests.make_fixtures import make_audio, make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, find_ffmpeg  # noqa: E402

PROJECT_NAME = "STEP5 검수 테스트"
FIXTURES = TEMP_DIR / "fixtures_step5"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def find(checks: list[qc.Check], name: str) -> qc.Check | None:
    return next((c for c in checks if c.name == name), None)


def build_project() -> dict:
    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(PROJECT_NAME, "dark_warning")

    for i, img in enumerate(make_images(FIXTURES / "images", 8)):
        rel = pm.save_upload(p, "images", f"{i + 1:02d}_full_product.jpg", img.read_bytes())
        cut = pm.new_cut("image", rel, img.name, 1.0, "slow_zoom_in")
        cut["scene_id"] = f"S{i + 1:02d}"
        cut["scene_role"] = "hook" if i == 0 else ("ending" if i == 7 else "evidence")
        cut["subtitle"] = f"자막 {i + 1}"
        p["cuts"].append(cut)
    vid = make_video(FIXTURES / "videos" / "clip.mp4", 4.0)
    rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
    cut = pm.new_cut("video", rel, vid.name, 1.2, "none")
    cut["scene_role"] = "process"
    cut["subtitle"] = "영상 컷"
    p["cuts"].insert(3, cut)

    voice = make_audio(FIXTURES / "voice.mp3", 9.5)
    rel = pm.save_upload(p, "voice", voice.name, voice.read_bytes())
    p["voice"] = {"file": rel, "duration": 9.5, "name": voice.name}
    bgm = make_audio(FIXTURES / "bgm.mp3", 25.0)
    rel = pm.save_upload(p, "bgm", bgm.name, bgm.read_bytes())
    p["bgm"] = {"file": rel, "name": bgm.name}

    p["ai_brief"] = {**sg.empty_brief(), "product_name": "테스트 제품", "target_length": 15}
    ma.analyze_project(p, use_vision=False)
    pm.save_project(p)
    return p


def test_pre_checks(project: dict) -> None:
    print("[1] 렌더 전 점검 — 정상 프로젝트")
    checks = qc.check_project(project)
    print("  " + qc.summary(checks))
    check("점검 항목 충분", len(checks) >= 12, f"{len(checks)}개")
    check("실패 항목 없음", not [c for c in checks if c.level == qc.FAIL],
          str([c.name for c in checks if c.level == qc.FAIL]))
    for name in ("첫 프레임", "첫 3초 컷", "해상도", "FPS", "마지막 블랙", "중복 연속", "정지컷"):
        check(f"점검 항목 존재: {name}", find(checks, name) is not None)
    check("해상도 통과", find(checks, "해상도").level == qc.PASS)
    check("FPS 통과", find(checks, "FPS").level == qc.PASS)


def test_pre_checks_problems(project: dict) -> None:
    print("[2] 렌더 전 점검 — 문제 감지")

    # 파일 누락
    broken = copy.deepcopy(project)
    broken["cuts"][0]["file"] = "media/images/does_not_exist.jpg"
    checks = qc.check_project(broken)
    item = find(checks, "파일 누락")
    check("파일 누락 감지", item is not None and item.level == qc.FAIL,
          item.message[:70] if item else "없음")

    # 컷 없음
    empty = copy.deepcopy(project)
    for cut in empty["cuts"]:
        cut["enabled"] = False
    checks = qc.check_project(empty)
    check("컷 없음 감지", any(c.level == qc.FAIL for c in checks), qc.summary(checks))

    # 첫 3초 컷 부족
    slow = copy.deepcopy(project)
    for cut in slow["cuts"]:
        cut["duration"] = 3.5
    checks = qc.check_project(slow)
    item = find(checks, "첫 3초 컷")
    check("첫 3초 컷 부족 감지", item is not None and item.level == qc.FAIL, item.message if item else "")

    # 첫 프레임 검정
    dark = copy.deepcopy(project)
    black_path = pm.abs_path(dark, pm.save_upload(dark, "images", "black.jpg", b""))
    subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=1080x1920:d=0.1", "-frames:v", "1", str(black_path)],
                   check=True)
    dark["cuts"][0]["file"] = black_path.relative_to(pm.project_dir(dark)).as_posix()
    checks = qc.check_project(dark)
    item = find(checks, "첫 프레임")
    check("첫 프레임 검정 감지", item is not None and item.level == qc.FAIL, item.message if item else "")

    # 음성 없음 / 자막 없음
    silent = copy.deepcopy(project)
    silent["voice"] = None
    for cut in silent["cuts"]:
        cut["subtitle"] = ""
    silent["auto_cues"] = []
    checks = qc.check_project(silent)
    check("음성 없음 경고", find(checks, "음성").level == qc.WARN)
    check("자막 없음 경고", find(checks, "자막").level == qc.WARN)
    check("음성/자막 없어도 실패는 아님", qc.worst(checks) != qc.FAIL, qc.summary(checks))

    # 중복 연속 사용
    dup = copy.deepcopy(project)
    dup["cuts"][1]["file"] = dup["cuts"][0]["file"]
    checks = qc.check_project(dup)
    item = find(checks, "중복 연속")
    check("중복 연속 감지", item is not None and item.level == qc.WARN, item.message[:60] if item else "")

    # 긴 정지컷
    still = copy.deepcopy(project)
    still["cuts"][2]["duration"] = 6.0
    still["cuts"][2]["effect"] = "none"
    checks = qc.check_project(still)
    item = find(checks, "정지컷")
    check("긴 정지컷 감지", item is not None and item.level == qc.WARN, item.message[:60] if item else "")

    # BGM 음량 과다
    loud = copy.deepcopy(project)
    loud["audio"]["bgm_volume"] = 0.8
    checks = qc.check_project(loud)
    item = find(checks, "BGM 음량")
    check("BGM 음량 과다 감지", item is not None and item.level == qc.WARN, item.message if item else "")

    # 시작 페이드
    fade = copy.deepcopy(project)
    fade["audio"]["fade_in"] = 0.8
    checks = qc.check_project(fade)
    item = find(checks, "시작 페이드")
    check("시작 페이드 경고", item is not None and item.level == qc.WARN, item.message if item else "")

    # 해상도/FPS 이상
    odd = copy.deepcopy(project)
    odd["width"], odd["height"], odd["fps"] = 720, 1280, 24
    checks = qc.check_project(odd)
    check("해상도 이상 감지", find(checks, "해상도").level == qc.FAIL)
    check("FPS 이상 감지", find(checks, "FPS").level == qc.WARN)

    # AI 생성 실패 장면
    pending = copy.deepcopy(project)
    script = sg.generate_rule_based(pending["ai_brief"])
    pending["script_data"] = script
    plan = sp.plan(pending, script, max_ai_clips=2)
    pending["scene_plan"] = plan
    ai = sp.ai_items(plan)
    if ai:
        sp.apply_generated(plan, ai[0]["scene_id"], "", False)
        checks = qc.check_project(pending)
        item = find(checks, "AI 영상")
        check("AI 생성 실패 장면 감지", item is not None and item.level == qc.WARN,
              item.message[:70] if item else "없음")


def test_post_checks(project: dict) -> None:
    print("[3] 렌더 후 결과 검사")
    output = renderer.render(project, preview=False)
    checks, frames = qc.check_output(project, output)
    print("  " + qc.summary(checks))
    for check_item in checks:
        print(f"    {check_item.icon} {check_item.name}: {check_item.message[:70]}")

    check("실패 항목 없음", not [c for c in checks if c.level == qc.FAIL],
          str([c.name for c in checks if c.level == qc.FAIL]))
    check("해상도 확인", find(checks, "해상도").level == qc.PASS)
    check("코덱 h264", find(checks, "코덱").level == qc.PASS)
    check("픽셀 포맷 yuv420p", find(checks, "픽셀 포맷").level == qc.PASS)
    check("오디오 코덱 aac", find(checks, "오디오 코덱").level == qc.PASS)
    check("길이 확인", find(checks, "길이") is not None)
    check("첫 프레임 통과", find(checks, "첫 프레임").level == qc.PASS,
          find(checks, "첫 프레임").message)
    check("클리핑 검사 수행", find(checks, "클리핑") is not None,
          find(checks, "클리핑").message if find(checks, "클리핑") else "")
    check("빈 화면 검사 수행", find(checks, "빈 화면") is not None)
    check("주요 시점 프레임 5장", len(frames) == 5, f"{len(frames)}장")
    check("프레임 파일 존재", all(p.is_file() for _, p in frames))
    check("프레임 라벨", "첫 프레임" in frames[0][0], frames[0][0] if frames else "")

    missing = qc.check_output(project, output.parent / "no_such_file.mp4")[0]
    check("없는 파일은 실패로 처리", missing[0].level == qc.FAIL)


def test_stats(project: dict) -> None:
    print("[4] 월간 통계")
    request = hf.GenerationRequest("S01", "prompt", "seedance_2_0", 5)
    ok = hf.GenerationResult(True, "S01", "ok", "media/generated/a.mp4", "job-1", "", 5.0, 30.0)
    hf.record_generation(project, request, ok, 1, 30.0)
    hf.record_generation(project, request,
                         hf.GenerationResult(False, "S02", "실패", "", "", "", 0.0, 0.0), 2, 30.0)
    pm.save_project(project)

    stats = hf.generation_stats([project])
    month = hf.month_key()
    check("월별 집계", month in stats)
    check("생성 횟수", stats[month]["generations"] == 2, str(stats[month]))
    check("성공/실패 구분", stats[month]["success"] == 1 and stats[month]["failed"] == 1)
    check("재생성 수", stats[month]["retries"] == 1)
    check("사용 크레딧", abs(stats[month]["credits"] - 30.0) < 0.01)
    check("영상당 평균", abs(stats[month]["avg_credits"] - 30.0) < 0.01)
    check("프로젝트 수", stats[month]["projects"] == 1)

    empty = hf.generation_stats([{"name": "x"}])
    check("기록 없으면 빈 통계", empty == {}, str(empty))


def main() -> int:
    print("STEP 5 테스트\n")
    project = build_project()
    test_pre_checks(project)
    test_pre_checks_problems(project)
    test_post_checks(project)
    test_stats(project)

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
