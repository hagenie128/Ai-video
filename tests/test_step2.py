"""STEP 2 테스트: 미디어 분석, 장면 계획, 자동 타임라인, 관심영역 렌더.

실행: python tests/test_step2.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import media_analyzer as ma  # noqa: E402
import project_manager as pm  # noqa: E402
import renderer  # noqa: E402
import scene_planner as sp  # noqa: E402
import script_generator as sg  # noqa: E402
import timeline as tl  # noqa: E402
from tests.make_fixtures import make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, media_info  # noqa: E402

PROJECT_NAME = "STEP2 테스트 프로젝트"
FIXTURES = TEMP_DIR / "fixtures_step2"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


BRIEF = {
    **sg.empty_brief(),
    "category": "가죽 지갑",
    "product_name": "반지갑",
    "description": "베지터블 가죽 반지갑입니다.",
    "benefits": "손바느질 마감\n금속 부속 없음",
    "facts": "가죽 두께 1.4mm",
    "target_length": 15,
    "content_type": "consumer_warning",
}

# 파일명 힌트가 태그로 이어지는지 확인하기 위한 이름
NAMED = [
    "01_full_product_front.jpg",
    "02_closeup_stitching.jpg",
    "03_comparison_logo.jpg",
    "04_workshop_hand.jpg",
    "05_detail_hardware.jpg",
    "06_leather_side.jpg",
    "07_text_label.jpg",
    "08_back_view.jpg",
    "09_full_product_2.jpg",
]


def build_project() -> dict:
    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(PROJECT_NAME, "dark_warning")

    raw = make_images(FIXTURES / "images", 9)
    for src, name in zip(raw, NAMED):
        rel = pm.save_upload(p, "images", name, src.read_bytes())
        p["cuts"].append(pm.new_cut("image", rel, name, 1.2, "none"))

    # 유사 이미지 2장 (거의 같은 사진) — 연속 사용 방지 확인용
    twin_dir = FIXTURES / "twins"
    twin_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(raw[0]) as base:
        a = twin_dir / "10_full_product_twin_a.jpg"
        b = twin_dir / "11_full_product_twin_b.jpg"
        base.save(a, quality=92)
        base.copy().rotate(0.4, expand=False).save(b, quality=92)
    for twin in (a, b):
        rel = pm.save_upload(p, "images", twin.name, twin.read_bytes())
        p["cuts"].append(pm.new_cut("image", rel, twin.name, 1.2, "none"))

    for i, seconds in enumerate((4.0, 5.0), start=1):
        vid = make_video(FIXTURES / "videos" / f"process_clip_{i}.mp4", seconds)
        rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
        cut = pm.new_cut("video", rel, vid.name, 1.5, "none")
        cut["source_duration"] = round(media_info(pm.abs_path(p, rel))["duration"], 2)
        p["cuts"].append(cut)

    # 손상 파일 (분석/렌더가 죽지 않아야 한다)
    broken = pm.abs_path(p, pm.save_upload(p, "images", "broken.jpg", b"not-an-image"))
    p["cuts"].append(pm.new_cut("image", broken.relative_to(pm.project_dir(p)).as_posix(),
                                "broken.jpg", 1.0, "none"))

    p["ai_brief"] = dict(BRIEF)
    pm.save_project(p)
    return p


def test_analysis(p: dict) -> dict:
    print("[1] 미디어 분석")
    analysis = ma.analyze_project(p, use_vision=False)
    check("모든 컷 분석", len(analysis) == len(p["cuts"]), f"{len(analysis)}/{len(p['cuts'])}")

    front = analysis[next(c["file"] for c in p["cuts"] if "full_product_front" in c["file"])]
    check("파일명 태그 인식 (full_product)", "full_product" in front["tags"], str(front["tags"]))
    stitch = analysis[next(c["file"] for c in p["cuts"] if "stitching" in c["file"])]
    check("파일명 태그 인식 (stitching)", "stitching" in stitch["tags"], str(stitch["tags"]))

    broken = analysis[next(c["file"] for c in p["cuts"] if "broken" in c["file"])]
    check("손상 파일 감지", bool(broken.get("error")), str(broken.get("error"))[:60])

    check("밝기 측정", 0.0 < front["brightness"] < 1.0, f"{front['brightness']}")
    check("품질 점수 범위", all(0.0 <= i.get("quality", 0) <= 1.0 for i in analysis.values()))

    twins = [analysis[c["file"]] for c in p["cuts"] if "twin" in c["file"]]
    check("유사 이미지 같은 그룹", len(twins) == 2 and twins[0]["similar_group"] == twins[1]["similar_group"],
          f"{[t['similar_group'] for t in twins]}")

    roles = {c["file"]: c.get("scene_role") for c in p["cuts"]}
    check("역할 자동 배정", sum(1 for r in roles.values() if r) >= len(p["cuts"]) - 1)
    check("역할 값 유효", all((r or "unused") in ma.ROLES for r in roles.values()), str(set(roles.values())))
    check("손상 파일은 unused", roles[next(c['file'] for c in p['cuts'] if 'broken' in c['file'])] == "unused")

    print("  " + ma.stats(p))
    return analysis


def test_plan(p: dict) -> list[dict]:
    print("[2] 장면 계획")
    script = sg.generate_rule_based(p["ai_brief"])
    p["script_data"] = script
    plan = sp.plan(p, script, max_ai_clips=3)
    check("계획 장면 수 = 대본 장면 수", len(plan) == len(script["scenes"]),
          f"{len(plan)} vs {len(script['scenes'])}")

    ai = sp.ai_items(plan)
    check("AI 장면 수 한도 이내", len(ai) <= 3, f"{len(ai)}개")
    check("AI 장면 프롬프트 있음", all(i["higgsfield_prompt"] for i in ai))
    check("AI 프롬프트에 금지 문구", all("Avoid:" in i["higgsfield_prompt"] for i in ai))
    check("AI 프롬프트에 로고 금지", all("no logo" in i["higgsfield_prompt"] for i in ai))
    check("AI 장면 모델 지정", all(i["model"] for i in ai), str([i["model"] for i in ai]))
    check("AI 장면 길이 3~6초", all(3 <= i["ai_seconds"] <= 6 for i in ai),
          str([i["ai_seconds"] for i in ai]))
    check("AI 장면은 사진 역할이 아닌 곳", all(i["role"] in sp.AI_FRIENDLY_ROLES for i in ai),
          str([i["role"] for i in ai]))

    non_ai = [i for i in plan if not i["needs_ai"]]
    check("모든 비AI 장면에 미디어 배정", all(i["media_file"] for i in non_ai),
          f"{sum(1 for i in non_ai if not i['media_file'])}개 미배정")

    broken_rel = next(c["file"] for c in p["cuts"] if "broken" in c["file"])
    check("손상 파일은 배치 제외", all(i["media_file"] != broken_rel for i in plan))

    unused = sp.unused_media(p, plan)
    check("모든 자료를 강제로 쓰지 않음", len(unused) >= 1, f"미사용 {len(unused)}개")

    # 유사 이미지가 연속으로 오지 않는지
    files = [i["media_file"] for i in plan if i["media_file"]]
    analysis = p["media_analysis"]
    groups = [analysis.get(f, {}).get("similar_group", -1) for f in files]
    consecutive = any(groups[i] >= 0 and groups[i] == groups[i + 1] for i in range(len(groups) - 1))
    check("유사 이미지 연속 배치 없음", not consecutive, str(groups))

    print("  " + sp.summary(p, plan))
    p["scene_plan"] = plan
    return plan


def test_timeline(p: dict, plan: list[dict]) -> None:
    print("[3] 자동 타임라인")
    fake_tts = {
        "gap": 0.12,
        "duration": 15.0,
        "segments": [{"scene_id": i["scene_id"], "text": i["voiceover"],
                      "start": 0, "end": 0, "duration": max(0.5, i["duration"])}
                     for i in plan],
    }
    count, message = tl.auto_build(p, plan, fake_tts)
    check("컷 배치됨", count >= 5, message)

    cuts = tl.enabled_cuts(p)
    check("첫 컷 역할 hook", cuts[0].get("scene_role") == "hook", str(cuts[0].get("scene_role")))
    check("마지막 컷 역할 ending", cuts[-1].get("scene_role") == "ending", str(cuts[-1].get("scene_role")))
    check("첫 3초 최소 3컷", tl.first_window_cuts(p) >= 3, f"{tl.first_window_cuts(p)}컷")

    images = sum(1 for c in cuts if c["type"] == "image")
    videos = sum(1 for c in cuts if c["type"] == "video")
    check("영상 컷도 사용됨 (사진/영상 교차)", videos >= 1, f"사진 {images} / 영상 {videos}")
    # 영상 수가 적으면 3연속을 물리적으로 피할 수 없다 → 이론적 최소값과 비교한다
    floor = max(2, -(-images // (videos + 1)))
    check("같은 타입 연속이 이론 최소값 이하", tl.type_runs(p) <= floor,
          f"최대 {tl.type_runs(p)}연속 (이론 최소 {floor})")
    check("시작 페이드 0", float(p["audio"]["fade_in"]) == 0.0)
    check("종료 페이드 0", float(p["audio"]["fade_out"]) == 0.0)
    check("마지막 블랙 0.8초", abs(float(p["audio"]["black_tail"]) - 0.8) < 0.001)
    check("미사용 자료 보존", any(not c.get("enabled") for c in p["cuts"]),
          f"{sum(1 for c in p['cuts'] if not c.get('enabled'))}개")
    check("미사용 자료 파일 남아 있음", all(
        pm.abs_path(p, c["file"]).is_file() for c in p["cuts"] if not c.get("enabled")))
    check("모든 컷에 자막 연결", sum(1 for c in cuts if c.get("subtitle")) >= len(cuts) - 1)

    stats = tl.stats(p)
    print(f"  컷 {stats['cuts']} (사진 {stats['images']}/영상 {stats['videos']}) · "
          f"첫3초 {stats['first_window']}컷 · 총 {stats['duration']}초 · "
          f"예상 렌더 {stats['render_estimate']}초")

    # 잠금 컷은 자동 재구성에서도 효과가 유지되어야 한다
    cuts[1]["locked"] = True
    cuts[1]["effect"] = "subtle_shake"
    locked_file = cuts[1]["file"]
    tl.auto_build(p, plan, fake_tts)
    same = next((c for c in tl.enabled_cuts(p) if c["file"] == locked_file), None)
    check("잠금 컷 효과 유지", same is not None and same.get("effect") == "subtle_shake",
          str(same.get("effect") if same else None))

    # 순서 번호 직접 이동
    before = [c["id"] for c in p["cuts"]]
    moved = tl.reorder(p["cuts"], before[3], 1)
    check("순서 번호 이동", moved and p["cuts"][0]["id"] == before[3])
    tl.reorder(p["cuts"], before[3], 4)
    pm.save_project(p)


def test_focus_render(p: dict) -> None:
    print("[4] 관심영역 / 효과 렌더")
    cuts = tl.enabled_cuts(p)
    images = [c for c in cuts if c["type"] == "image"]
    check("사진 컷 존재", bool(images))
    if not images:
        return
    for cut, effect, focus, xy in zip(
        images,
        ["slow_zoom_in", "fast_zoom_in", "slow_zoom_out", "pan_left_to_right", "subtle_shake"],
        ["custom", "top", "right", "custom", "center"],
        [[0.28, 0.72], [0.5, 0.25], [0.75, 0.5], [0.8, 0.2], [0.5, 0.5]],
    ):
        cut["effect"] = effect
        cut["focus"] = focus
        cut["focus_xy"] = xy

    videos = [c for c in cuts if c["type"] == "video"]
    if videos:
        videos[0]["focus"] = "custom"
        videos[0]["focus_xy"] = [0.3, 0.7]

    # 표현식 생성 확인
    vf = renderer.build_image_filter(p, "slow_zoom_in", 30, 1080, 1920, 30, images[0])
    check("초점 표현식 포함", "min(iw-iw/zoom" in vf, vf[:120])
    vf_center = renderer.build_image_filter(p, "slow_zoom_in", 30, 1080, 1920, 30,
                                            {"focus": "center", "focus_xy": [0.5, 0.5]})
    check("가운데는 기존 표현식 유지", "(iw-iw/zoom)/2" in vf_center)
    if videos:
        vfv = renderer.build_video_filter(p, 30, 1080, 1920, 30, videos[0])
        check("영상 crop 초점 반영", "crop=w=1080" in vfv, vfv[:100])

    print("  ... 미리보기 렌더링")
    out = renderer.render(p, preview=True)
    check("초점/효과 적용 렌더 성공", out.is_file())
    info = media_info(out)
    check("미리보기 해상도 540x960", (info["width"], info["height"]) == (540, 960),
          f"{info['width']}x{info['height']}")

    print("  ... 최종 렌더링")
    final = renderer.render(p, preview=False)
    info = media_info(final)
    check("최종 1080x1920", (info["width"], info["height"]) == (1080, 1920))
    check("오디오 스트림 존재", info["has_audio"])
    expected = tl.total_duration(p)
    check("길이 오차 0.4초 이내", abs(info["duration"] - expected) < 0.4,
          f"실제 {info['duration']:.2f}s / 예상 {expected:.2f}s")


def main() -> int:
    print("STEP 2 테스트\n")
    p = build_project()
    test_analysis(p)
    plan = test_plan(p)
    test_timeline(p, plan)
    test_focus_render(p)

    reloaded = pm.load_project(p["name"])
    check("저장 후 재불러오기", len(reloaded["cuts"]) == len(p["cuts"])
          and bool(reloaded.get("scene_plan")))

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
