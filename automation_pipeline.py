"""전체 자동 제작 파이프라인 (9단계).

원칙
- 한 단계가 실패해도 전체를 중단하지 않는다. 가능한 단계는 계속 진행한다.
- 실패한 단계는 '수동 작업 필요' 로 표시하고, 오류 메시지와 다음 행동을 알려준다.
- start_at 을 주면 그 단계부터 다시 실행한다 (앞 단계 결과는 그대로 재사용).
- 모든 단계 결과는 project["automation_log"] 에 남는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import audio_library as al
import config
import higgsfield_prompt_builder as hpb
import higgsfield_service as hf
import media_analyzer as ma
import project_manager as pm
import renderer
import scene_planner as sp
import script_generator as sg
import timeline as tl
import tts_service
from renderer import RenderError

STEPS = [
    ("analyze", "자료 분석"),
    ("script", "대본 생성"),
    ("scene_plan", "장면 구성"),
    ("prompts", "Higgsfield 프롬프트 생성"),
    ("higgsfield", "AI 영상 생성"),
    ("tts", "TTS 생성"),
    ("timeline", "자동 편집"),
    ("audio_subtitle", "자막·오디오 합성"),
    ("render", "최종 렌더링"),
]
STEP_KEYS = [k for k, _ in STEPS]

Progress = Callable[[int, str, str], None]        # (단계 번호, 단계명, 상태 메시지)


def default_options() -> dict:
    return {
        "script": True,
        "tts": True,
        "higgsfield": False,        # 크레딧이 드는 단계라 기본은 꺼짐
        "subtitles": True,
        "bgm": True,
        "sfx": True,
        "timeline": True,
        "render": True,
        "vision": True,
        "max_ai_clips": None,       # None 이면 설정값 사용
    }


@dataclass
class StepResult:
    key: str
    label: str
    status: str = "skipped"        # ok | failed | skipped | manual
    message: str = ""
    detail: str = ""

    @property
    def icon(self) -> str:
        return {"ok": "✅", "failed": "❌", "manual": "✋", "skipped": "⏭"}.get(self.status, "•")


@dataclass
class PipelineResult:
    steps: list[StepResult] = field(default_factory=list)
    output: Path | None = None
    manual_actions: list[str] = field(default_factory=list)

    def get(self, key: str) -> StepResult | None:
        return next((s for s in self.steps if s.key == key), None)

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.status in ("failed", "manual")]

    def summary(self) -> str:
        ok = sum(1 for s in self.steps if s.status == "ok")
        return (f"{ok}/{len(self.steps)} 단계 완료"
                + (f" · 확인 필요 {len(self.failed)}건" if self.failed else "")
                + (f" · 출력 {self.output.name}" if self.output else ""))


def _log(project: dict, step: str, ok: bool, message: str) -> None:
    project.setdefault("automation_log", []).append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "step": step, "ok": bool(ok), "message": (message or "")[:500],
    })


# ---------------------------------------------------------------- 단계 구현

def _step_analyze(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("analyze", "자료 분석")
    if not project.get("cuts"):
        s.status = "manual"
        s.message = "업로드된 사진/영상이 없습니다. 자료를 먼저 등록하세요."
        ctx["manual"].append("사진 또는 영상을 등록하세요.")
        return s
    try:
        ma.analyze_project(project, use_vision=bool(opts["vision"]))
        s.status = "ok"
        s.message = ma.stats(project)
    except Exception as exc:  # noqa: BLE001 - 분석 실패로 전체를 멈추지 않는다
        s.status = "failed"
        s.message = f"분석 실패: {exc}"
    return s


def _step_script(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("script", "대본 생성")
    existing = (project.get("script_data") or {}).get("scenes")
    if not opts["script"] and existing:
        s.status = "ok"
        s.message = "기존 대본을 사용합니다."
        return s
    try:
        summary = ma.summary_for_prompt(project) if project.get("media_analysis") else ""
        script = sg.generate(brief, summary, _sample_images(project, 6))
        project["script_data"] = script
        project["script"] = script.get("full_script", "")
        s.status = "ok"
        s.message = sg.summary_text(script)
        if script.get("fallback_reason"):
            s.detail = script["fallback_reason"]
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"대본 생성 실패: {exc}"
        ctx["manual"].append("대본을 직접 입력하거나 다시 생성하세요.")
    return s


def _step_scene_plan(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("scene_plan", "장면 구성")
    script = project.get("script_data") or {}
    if not script.get("scenes"):
        s.status = "manual"
        s.message = "대본 장면이 없어 장면 구성을 건너뜁니다."
        return s
    try:
        plan_items = sp.plan(project, script, max_ai_clips=opts.get("max_ai_clips"))
        project["scene_plan"] = plan_items
        s.status = "ok" if plan_items else "failed"
        s.message = sp.summary(project, plan_items)
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"장면 구성 실패: {exc}"
    return s


def _step_prompts(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("prompts", "Higgsfield 프롬프트 생성")
    plan_items = project.get("scene_plan") or []
    script = project.get("script_data") or {}
    scenes = script.get("scenes") or []
    ai_list = sp.ai_items(plan_items)
    if not ai_list:
        s.status = "skipped"
        s.message = "AI 영상이 필요한 장면이 없습니다 (사진/영상만으로 구성)."
        return s
    try:
        filled = 0
        for item in ai_list:
            if item.get("higgsfield_prompt") and "Avoid:" in item["higgsfield_prompt"]:
                continue
            scene = next((sc for sc in scenes if sc["scene_id"] == item["scene_id"]), None)
            if scene is None:
                continue
            item["higgsfield_prompt"] = hpb.build_prompt(
                scene, brief, bool(item.get("reference_image")), item.get("music", ""))
            filled += 1
        hpb.refresh_scene_prompts(script, brief)
        s.status = "ok"
        s.message = (f"AI 장면 {len(ai_list)}개 프롬프트 준비 (새로 만든 것 {filled}개) · "
                     f"예상 {sp.total_estimated_credits(plan_items):.1f} 크레딧")
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"프롬프트 생성 실패: {exc}"
    return s


def _step_higgsfield(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("higgsfield", "AI 영상 생성")
    plan_items = project.get("scene_plan") or []
    ai_list = [i for i in sp.ai_items(plan_items) if i.get("status") != "ai_ready"]
    if not sp.ai_items(plan_items):
        s.status = "skipped"
        s.message = "AI 영상 장면이 없습니다."
        return s
    if not ai_list:
        s.status = "ok"
        s.message = "AI 영상이 이미 모두 준비되어 있습니다."
        return s
    if not opts["higgsfield"]:
        s.status = "manual"
        s.message = (f"AI 영상 자동 생성이 꺼져 있습니다. {len(ai_list)}개 장면은 임시 사진으로 대체됩니다. "
                     "'Higgsfield AI 영상' 섹션에서 승인 후 생성하거나 수동 업로드하세요.")
        ctx["manual"].append(f"AI 영상 {len(ai_list)}개 장면: 승인 후 생성 또는 수동 업로드")
        return s
    s.status, s.message, s.detail = _run_higgsfield(project, plan_items, ai_list, report)
    if s.status != "ok":
        ctx["manual"].append("AI 영상이 없는 장면은 수동 업로드로 채우거나 사진으로 대체하세요.")
    return s


def _step_tts(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("tts", "TTS 생성")
    scenes = (project.get("script_data") or {}).get("scenes") or []
    if (project.get("tts") or {}).get("provider") == "upload" and project.get("voice"):
        s.status = "ok"
        s.message = f"직접 업로드한 음성을 사용합니다 ({project['voice'].get('duration', 0):.2f}초)."
        return s
    if not opts["tts"]:
        s.status = "skipped"
        s.message = "TTS 자동 생성이 꺼져 있습니다."
        return s
    if not scenes:
        s.status = "manual"
        s.message = "대본이 없어 음성을 만들 수 없습니다."
        return s
    try:
        def tts_progress(fraction: float, message: str) -> None:
            report(5, message)

        tts = tts_service.synthesize_scenes(project, scenes, progress_cb=tts_progress)
        project["tts"] = tts
        project["voice"] = {"file": tts["file"], "duration": tts["duration"], "name": tts["name"]}
        project["auto_cues"] = tts_service.cues_from_segments(
            tts["segments"], int((project.get("subtitle") or {}).get("max_chars", 14)))
        s.status = "ok"
        s.message = tts_service.length_advice(tts["duration"], _target_seconds(brief))
    except (tts_service.TTSError, tts_service.TTSUnavailable) as exc:
        s.status = "manual"
        s.message = f"음성 자동 생성 실패: {exc}"
        s.detail = "자료 섹션에서 음성 파일을 직접 업로드하면 계속 진행할 수 있습니다."
        ctx["manual"].append("음성 파일을 직접 업로드하세요 (TTS 실패).")
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"음성 생성 중 오류: {exc}"
        ctx["manual"].append("음성 파일을 직접 업로드하세요.")
    return s


def _step_timeline(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("timeline", "자동 편집")
    plan_items = project.get("scene_plan") or []
    if not opts["timeline"]:
        s.status = "skipped"
        s.message = "자동 타임라인 구성이 꺼져 있습니다."
        return s
    if not plan_items:
        s.status = "manual"
        s.message = "장면 계획이 없어 타임라인을 만들 수 없습니다."
        return s
    try:
        count, message = tl.auto_build(project, plan_items, project.get("tts") or None)
        s.status = "ok" if count else "failed"
        s.message = message
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"타임라인 구성 실패: {exc}"
    return s


def _step_audio_subtitle(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("audio_subtitle", "자막·오디오 합성")
    scenes = (project.get("script_data") or {}).get("scenes") or []
    notes: list[str] = []
    try:
        if opts["subtitles"]:
            cues = project.get("auto_cues") or []
            if cues:
                project.setdefault("subtitle", {})["mode"] = "auto"
                notes.append(f"자동 자막 {len(cues)}개 (TTS 발화 기준)")
            else:
                project.setdefault("subtitle", {})["mode"] = "cut"
                notes.append("TTS 타이밍이 없어 컷 자막을 사용합니다.")
        else:
            notes.append("자동 자막이 꺼져 있습니다.")

        if opts["bgm"] or opts["sfx"]:
            timings = {seg["scene_id"]: (seg["start"], seg["end"])
                       for seg in (project.get("tts") or {}).get("segments") or []}
            audio_report = al.apply_to_project(
                project, brief.get("content_type", "product_intro"), scenes, timings,
                use_music=bool(opts["bgm"]), use_sfx=bool(opts["sfx"]),
                mood_hint=str(brief.get("mood") or ""))
            notes += audio_report["messages"]
        s.status = "ok"
        s.message = " · ".join(notes)
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"자막/오디오 합성 실패: {exc}"
    return s


def _step_render(project, opts, brief, ctx, report) -> StepResult:
    s = StepResult("render", "최종 렌더링")
    if not opts["render"]:
        s.status = "skipped"
        s.message = "최종 렌더링이 꺼져 있습니다. '7. 출력' 화면에서 직접 렌더링할 수 있습니다."
        return s
    if not tl.enabled_cuts(project):
        s.status = "manual"
        s.message = "사용 중인 컷이 없어 렌더링할 수 없습니다."
        return s
    try:
        def render_progress(fraction: float, message: str) -> None:
            report(8, f"{message} ({fraction * 100:.0f}%)")

        output = renderer.render(project, preview=bool(ctx.get("preview")), progress_cb=render_progress)
        project["last_output"] = str(output)
        ctx["output"] = output
        s.status = "ok"
        s.message = f"렌더링 완료: {output.name}"
    except RenderError as exc:
        s.status = "failed"
        s.message = f"렌더링 실패: {exc}"
        s.detail = exc.detail
        ctx["manual"].append("렌더링 오류를 확인하고 '7. 출력'에서 다시 시도하세요.")
    except RuntimeError as exc:
        s.status = "failed"
        s.message = str(exc)
    except Exception as exc:  # noqa: BLE001
        s.status = "failed"
        s.message = f"렌더링 중 예상치 못한 오류: {exc}"
    return s


_HANDLERS = {
    "analyze": _step_analyze,
    "script": _step_script,
    "scene_plan": _step_scene_plan,
    "prompts": _step_prompts,
    "higgsfield": _step_higgsfield,
    "tts": _step_tts,
    "timeline": _step_timeline,
    "audio_subtitle": _step_audio_subtitle,
    "render": _step_render,
}


# ---------------------------------------------------------------- 실행

def run(
    project: dict,
    options: dict | None = None,
    progress: Progress | None = None,
    preview: bool = False,
    start_at: str = "",
) -> PipelineResult:
    """전체 자동 제작. 실패한 단계는 건너뛰고 계속 진행한다."""
    opts = {**default_options(), **(options or {})}
    brief = {**sg.empty_brief(), **(project.get("ai_brief") or {})}
    project["ai_brief"] = brief

    if start_at and start_at not in STEP_KEYS:
        raise ValueError(f"알 수 없는 단계: {start_at}")
    start_index = STEP_KEYS.index(start_at) if start_at else 0

    result = PipelineResult()
    ctx: dict = {"manual": [], "output": None, "preview": preview}

    def report(index: int, message: str) -> None:
        if progress:
            progress(index, STEPS[index][1], message)

    for index, (key, label) in enumerate(STEPS):
        if index < start_index:
            item = StepResult(key, label, "skipped", "이전 결과를 그대로 사용합니다.")
            result.steps.append(item)
            continue
        report(index, "진행 중")
        try:
            item = _HANDLERS[key](project, opts, brief, ctx, report)
        except Exception as exc:  # noqa: BLE001 - 어떤 단계도 전체를 멈추지 않는다
            item = StepResult(key, label, "failed", f"예상치 못한 오류: {exc}")
        result.steps.append(item)
        _log(project, key, item.status == "ok", item.message)
        report(index, f"{item.icon} {item.message[:80]}")
        # 중간 저장: 뒤 단계가 실패해도 앞 결과는 남는다
        try:
            pm.save_project(project)
        except OSError:
            pass

    result.output = ctx.get("output")
    result.manual_actions = ctx["manual"]
    return result


def retry_from(project: dict, key: str, options: dict | None = None,
               progress: Progress | None = None, preview: bool = False) -> PipelineResult:
    """실패한 단계부터 다시 실행한다. 앞 단계 결과는 그대로 재사용한다."""
    opts = {**default_options(), **(options or {})}
    if key != "analyze":
        opts["vision"] = False
    if key != "script":
        opts["script"] = False
    return run(project, opts, progress, preview, start_at=key)


# ---------------------------------------------------------------- 내부

def _run_higgsfield(project: dict, plan_items: list[dict], ai_list: list[dict],
                    report: Callable[[int, str], None]) -> tuple[str, str, str]:
    """AI 영상 생성. 승인 규칙과 예산을 지키고, 실패 장면은 수동으로 넘긴다."""
    settings = config.load_settings()["higgsfield"]
    max_attempts = int(settings.get("max_attempts", 2))

    if settings.get("mode") != "cli":
        return ("manual",
                f"연동 방식이 '{settings.get('mode')}' 입니다. 프롬프트를 복사해 영상을 만든 뒤 수동 업로드하세요.",
                "")

    env = hf.environment_status()
    if not env["cli"]:
        return ("manual", f"Higgsfield CLI 가 없습니다. 설치: {hf.INSTALL_COMMAND}",
                "설치 없이도 수동 업로드로 진행할 수 있습니다.")
    if env["auth"] == "no":
        return ("manual", f"로그인이 필요합니다: {hf.LOGIN_COMMAND}", "")
    if not env.get("can_generate"):
        return ("manual", "설치된 CLI 에서 영상 생성 명령을 확인하지 못했습니다.",
                "문법을 추측하지 않고 수동 업로드로 전환합니다.")

    done, failed, skipped = 0, 0, 0
    details: list[str] = []
    for item in ai_list:
        estimated = float(item.get("estimated_credits") or 0.0)
        over, reason = hf.over_budget(project, estimated)
        if over:
            skipped += 1
            details.append(f"{item['scene_id']}: {reason}")
            continue
        need, reason = hf.needs_approval(project, estimated)
        if need:
            skipped += 1
            details.append(f"{item['scene_id']}: 승인 필요 — {reason}")
            continue
        if int(item.get("attempts", 0)) >= max_attempts:
            skipped += 1
            details.append(f"{item['scene_id']}: 최대 시도 횟수({max_attempts}회) 도달")
            continue

        request = hf.GenerationRequest(
            scene_id=item["scene_id"],
            prompt=item.get("higgsfield_prompt", ""),
            model=item.get("model", ""),
            duration=float(item.get("ai_seconds") or settings.get("default_duration", 5)),
            aspect_ratio=settings.get("aspect_ratio", "9:16"),
            with_audio=bool(settings.get("with_audio", False)),
            image_path=_reference_path(project, item),
        )
        report(4, f"{item['scene_id']} AI 영상 생성 중")
        try:
            generated = hf.generate_clip(project, request)
        except (hf.HiggsfieldUnavailable, hf.HiggsfieldUnsupported) as exc:
            return "manual", str(exc), "수동 업로드로 전환하세요."

        item["attempts"] = int(item.get("attempts", 0)) + 1
        hf.record_generation(project, request, generated, item["attempts"], estimated)
        if generated.ok:
            sp.apply_generated(plan_items, item["scene_id"], generated.file, True)
            done += 1
        else:
            item["status"] = "ai_failed"
            failed += 1
            details.append(f"{item['scene_id']}: {generated.message[:160]}")
        pm.save_project(project)

    message = f"생성 완료 {done}개 · 실패 {failed}개 · 승인/예산 대기 {skipped}개"
    status = "ok" if done and not failed and not skipped else ("manual" if done or skipped else "failed")
    return status, message, "\n".join(details[:8])


def _reference_path(project: dict, item: dict) -> Path | None:
    if not item.get("reference_image"):
        return None
    path = pm.abs_path(project, item["reference_image"])
    return path if path.is_file() else None


def _sample_images(project: dict, limit: int) -> list[Path]:
    paths: list[Path] = []
    for cut in project.get("cuts", []):
        if cut.get("type") != "image":
            continue
        path = pm.abs_path(project, cut["file"])
        if path.is_file():
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _target_seconds(brief: dict) -> float | None:
    try:
        return float(brief.get("target_length") or 0) or None
    except (TypeError, ValueError):
        return None
