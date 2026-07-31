"""'0. AI 자동 제작' 화면의 Higgsfield 영상 생성 섹션.

- 생성 전 장면명/프롬프트/모델/길이/예상 크레딧/사용 이미지를 보여주고 승인을 받는다.
- 자동 승인 설정이어도 영상당 한도를 넘으면 반드시 승인을 받는다.
- CLI 가 없거나 미인증/미지원이면 수동 업로드 대기 상태로 전환한다 (프롬프트 복사 제공).
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

import config
import higgsfield_service as hf
import project_manager as pm
import scene_planner as sp
from utils import VIDEO_EXTS

STATUS_LABEL = {
    "ai_pending": "생성 대기",
    "ai_ready": "생성 완료",
    "ai_failed": "생성 실패",
    "photo_fallback": "사진으로 대체",
}


def _log(project: dict, step: str, ok: bool, message: str) -> None:
    from datetime import datetime
    project.setdefault("automation_log", []).append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "step": step, "ok": bool(ok), "message": message[:500],
    })


def _apply_to_timeline(project: dict, scene_id: str, rel_file: str) -> None:
    """생성된 영상을 이미 만들어진 타임라인 컷에도 반영한다."""
    for cut in project.get("cuts", []):
        if cut.get("scene_id") != scene_id:
            continue
        cut["file"] = rel_file
        cut["type"] = "video"
        cut["name"] = Path(rel_file).name
        cut["effect"] = "none"
        cut["source"] = "higgsfield"
        cut["generation_status"] = "success"
        try:
            from utils import media_info
            cut["source_duration"] = round(media_info(pm.abs_path(project, rel_file))["duration"], 2)
        except Exception:  # noqa: BLE001 - 길이를 못 읽어도 렌더는 가능하다
            cut["source_duration"] = 0.0


def _register_cut_for_scene(project: dict, item: dict, rel_file: str) -> None:
    """타임라인에 해당 장면 컷이 없으면 새로 추가한다."""
    if any(c.get("scene_id") == item["scene_id"] for c in project.get("cuts", [])):
        _apply_to_timeline(project, item["scene_id"], rel_file)
        return
    cut = pm.new_cut("video", rel_file, Path(rel_file).name, max(0.6, item.get("duration", 1.5)), "none")
    cut["scene_id"] = item["scene_id"]
    cut["scene_role"] = item["role"]
    cut["subtitle"] = item.get("subtitle", "")
    cut["source"] = "higgsfield"
    cut["generation_status"] = "success"
    project.setdefault("cuts", []).append(cut)


def section(project: dict) -> None:
    st.subheader("⑦ Higgsfield AI 영상")
    plan_items = project.get("scene_plan") or []
    ai_list = sp.ai_items(plan_items)

    settings = config.load_settings()
    cfg = settings["higgsfield"]
    mode = cfg.get("mode", "cli")

    if not plan_items:
        st.info("먼저 장면 계획을 만드세요.")
        return
    if not ai_list:
        st.success("AI 영상이 필요한 장면이 없습니다. 업로드한 사진과 영상만으로 구성됩니다.")
        return

    env = hf.environment_status()
    budget = hf.budget_state(project)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AI 장면", f"{len(ai_list)}개")
    c2.metric("예상 크레딧", f"{sp.total_estimated_credits(plan_items):.1f}")
    c3.metric("이번 달 사용", f"{budget['used']:.1f}")
    c4.metric("계정 잔여", "확인 안 됨" if env.get("credits") is None else f"{env['credits']:.1f}")

    if mode == "cli" and not env["cli"]:
        st.warning(f"Higgsfield CLI 가 없습니다. 설치하면 자동 생성이 가능합니다: `{hf.INSTALL_COMMAND}`\n\n"
                   "지금은 아래 '수동 업로드'로 진행할 수 있습니다.")
    elif mode == "cli" and env["auth"] == "no":
        st.warning(f"로그인이 필요합니다. 터미널에서 `{hf.LOGIN_COMMAND}` 실행 후 "
                   "'8. AI 연동 설정'에서 상태를 다시 확인하세요.")
    elif mode == "cli" and not env.get("can_generate"):
        st.warning("설치된 CLI 에서 영상 생성 명령을 확인하지 못했습니다. 수동 업로드로 진행하세요.")
    elif mode == "mcp":
        st.info("MCP 방식은 Claude 쪽에서 별도로 사용합니다. 이 화면에서는 프롬프트를 복사해 사용하고, "
                "만든 영상을 아래 '수동 업로드'로 등록하세요.")
    elif mode == "manual":
        st.info("수동 업로드 모드입니다. 아래 프롬프트로 영상을 만든 뒤 등록하세요.")

    can_auto = mode == "cli" and env["cli"] and env.get("can_generate") and env["auth"] != "no"

    over, reason = hf.over_budget(project, sp.total_estimated_credits(plan_items))
    if over:
        st.error(reason)

    st.caption("웹에서만 제공되는 무료/무제한 모델은 CLI/MCP 에서 쓸 수 없는 경우가 있습니다. "
               "그럴 때는 웹에서 만든 영상을 수동 업로드로 등록하세요.")

    if can_auto:
        c1, c2 = st.columns([1, 3])
        if c1.button("전체 승인 후 생성", type="primary", key="hf_gen_all", disabled=over):
            _generate_batch(project, plan_items,
                            [i for i in ai_list if i["status"] in ("ai_pending", "ai_failed")])
        c2.caption("장면마다 승인 여부를 따로 정하려면 아래 각 장면의 '승인 후 생성'을 쓰세요. "
                   f"현재 승인 방식: {'매번 승인' if cfg.get('approval') == 'always' else '예산 이하 자동 승인'}")

    for item in ai_list:
        _scene_card(project, plan_items, item, can_auto, env)

    pending = sp.scenes_needing_manual(plan_items)
    if pending:
        st.divider()
        st.warning(f"AI 영상이 아직 없는 장면 {len(pending)}개가 있습니다. "
                   "수동 업로드로 채우거나, 실제 사진으로 대체할 수 있습니다.")
        c1, c2 = st.columns(2)
        if c1.button("남은 장면을 실제 사진으로 대체", key="hf_fallback_all", type="primary",
                     width="stretch"):
            changed = sp.fallback_ai_to_photo(project, plan_items)
            _log(project, "higgsfield_fallback", True, f"{changed}개 장면 사진 대체")
            pm.save_project(project)
            st.success(f"{changed}개 장면을 사진으로 대체했습니다. '쇼츠 초안 자동 구성'을 다시 실행하세요.")
            st.rerun()
        exhausted_items = [i for i in pending
                           if int(i.get("attempts", 0)) >= int(
                               config.load_settings()["higgsfield"].get("max_attempts", 2))]
        if exhausted_items and c2.button(
                f"시도 횟수 초기화 ({len(exhausted_items)}개 장면)", key="hf_reset_all",
                width="stretch", help="로그인·설치·예산 등 실패 원인을 고친 뒤에 쓰세요."):
            for entry in exhausted_items:
                entry["attempts"] = 0
                entry["status"] = "ai_pending"
            _log(project, "higgsfield_reset", True, f"{len(exhausted_items)}개 장면 시도 횟수 초기화")
            pm.save_project(project)
            st.rerun()


def _last_failure(project: dict, scene_id: str) -> str:
    """이 장면의 마지막 실패 사유 (생성 기록에서 찾는다)."""
    for record in reversed(project.get("higgsfield_generations") or []):
        if record.get("scene_id") != scene_id:
            continue
        if record.get("status") == "success":
            return ""
        message = str(record.get("message") or "").strip()
        return " ".join(message.split())[:300] if message else "사유가 기록되지 않았습니다."
    return ""


def _scene_card(project: dict, plan_items: list[dict], item: dict, can_auto: bool, env: dict) -> None:
    scene_id = item["scene_id"]
    settings = config.load_settings()["higgsfield"]
    max_attempts = int(settings.get("max_attempts", 2))
    attempts = int(item.get("attempts", 0))
    status = item.get("status", "ai_pending")

    with st.container(border=True):
        head, body = st.columns([1.4, 4.6])
        with head:
            st.markdown(f"**{scene_id}** · {item['role']}")
            st.caption(f"모델 `{item['model']}`")
            st.caption(f"{item['ai_seconds']}초 · 9:16 · 오디오 "
                       f"{'포함' if settings.get('with_audio') else 'Off'}")
            st.caption(f"예상 {item['estimated_credits']:.1f} 크레딧")
            st.caption(f"시도 {attempts}/{max_attempts} · {STATUS_LABEL.get(status, status)}")
            if item.get("reference_image"):
                path = pm.abs_path(project, item["reference_image"])
                if path.is_file():
                    st.image(str(path), width="stretch", caption="참고 이미지 (구조/소재만)")

        with body:
            item["higgsfield_prompt"] = st.text_area(
                "프롬프트", value=item.get("higgsfield_prompt", ""), height=120,
                key=f"hfp_{scene_id}")
            st.caption(f"자막: {item.get('subtitle', '').replace(chr(10), ' ')}")

            if status == "ai_ready" and item.get("media_file"):
                path = pm.abs_path(project, item["media_file"])
                if path.is_file():
                    st.video(str(path))
                st.success(f"생성 완료: `{item['media_file']}`")

            # 왜 실패했는지 보여준다 (기록이 있으면 마지막 실패 사유)
            reason = _last_failure(project, scene_id)
            if reason and status != "ai_ready":
                st.error(f"마지막 실패 사유 — {reason}")

            exhausted = attempts >= max_attempts and status != "ai_ready"
            if exhausted:
                st.warning(
                    f"최대 시도 횟수({max_attempts}회)에 도달했습니다. 아래 중 하나를 고르세요.\n\n"
                    "· **수동 업로드** — Higgsfield 웹/MCP 로 만든 영상을 올립니다 (가장 확실)\n"
                    "· **사진으로 대체** — 이 장면을 실제 사진으로 채웁니다 (한 번에 끝)\n"
                    "· **시도 횟수 초기화** — 실패 원인(로그인·설치·예산 등)을 고쳤을 때만"
                )

            c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 2.4])
            if can_auto:
                label = "승인 후 생성" if status != "ai_ready" else "재생성"
                if c1.button(label, key=f"hfg_{scene_id}", type="primary", disabled=exhausted):
                    _generate_batch(project, plan_items, [item], force_scene=scene_id)
                if exhausted:
                    c1.caption(f"최대 시도({max_attempts}회) 도달")
                if c2.button("실제 견적 조회", key=f"hfc_{scene_id}"):
                    request = _build_request(project, item)
                    with st.spinner("견적 조회 중..."):
                        value, note = hf.estimate_credits_live(request)
                    if value is None:
                        st.info(note)
                    else:
                        item["estimated_credits"] = value
                        pm.save_project(project)
                        st.success(note)
                if status != "ai_ready":
                    need, reason = hf.needs_approval(project, float(item["estimated_credits"]))
                    c4.caption(("⚠ " if need else "✓ ") + reason)
            if c3.button("사진으로 대체", key=f"hff_{scene_id}"):
                item["status"] = "ai_failed"
                changed = sp.fallback_ai_to_photo(project, [item])
                pm.save_project(project)
                st.info(f"{changed}개 장면을 사진으로 대체했습니다.")
                st.rerun()

            if exhausted and can_auto:
                if c2.button("시도 횟수 초기화", key=f"hfz_{scene_id}"):
                    item["attempts"] = 0
                    item["status"] = "ai_pending"
                    _log(project, "higgsfield_reset", True, f"{scene_id} 시도 횟수 초기화")
                    pm.save_project(project)
                    st.rerun()
                c2.caption("이미 쓴 크레딧은 돌아오지 않습니다.")

            with st.expander("수동 업로드 (Plan B)", expanded=not can_auto and status != "ai_ready"):
                st.code(item.get("higgsfield_prompt", ""), language="text")
                st.caption("위 프롬프트를 Higgsfield 웹/MCP 에 붙여 영상을 만든 뒤 아래에 올리세요. "
                           "올린 영상은 이 장면에 자동으로 연결됩니다.")
                upload = st.file_uploader(
                    f"{scene_id} 영상 업로드", type=[e.lstrip(".") for e in sorted(VIDEO_EXTS)],
                    key=f"hfu_{scene_id}")
                if upload is not None and st.button("이 장면에 등록", key=f"hfr_{scene_id}"):
                    result = hf.register_manual_upload(project, scene_id, upload.name, upload.getvalue())
                    if result.ok:
                        sp.apply_generated(plan_items, scene_id, result.file, True)
                        _register_cut_for_scene(project, item, result.file)
                        request = _build_request(project, item)
                        hf.record_generation(project, request, result, int(item.get("attempts", 1)), 0.0)
                        _log(project, "higgsfield_manual", True, f"{scene_id} 수동 등록")
                        pm.save_project(project)
                        st.success(result.message)
                        st.rerun()
                    else:
                        st.error(result.message)


def _build_request(project: dict, item: dict) -> hf.GenerationRequest:
    settings = config.load_settings()["higgsfield"]
    image = None
    if item.get("reference_image"):
        path = pm.abs_path(project, item["reference_image"])
        if path.is_file():
            image = path
    return hf.GenerationRequest(
        scene_id=item["scene_id"],
        prompt=item.get("higgsfield_prompt", ""),
        model=item.get("model", ""),
        duration=float(item.get("ai_seconds") or settings.get("default_duration", 5)),
        aspect_ratio=settings.get("aspect_ratio", "9:16"),
        with_audio=bool(settings.get("with_audio", False)),
        image_path=image,
    )


def _generate_batch(project: dict, plan_items: list[dict], items: list[dict],
                    force_scene: str | None = None) -> None:
    """승인 확인 후 순서대로 생성한다. 실패해도 다음 장면을 계속 진행한다."""
    settings = config.load_settings()["higgsfield"]
    max_attempts = int(settings.get("max_attempts", 2))
    approved_key = "_hf_approved"
    approved: set[str] = set(st.session_state.get(approved_key, set()))

    for item in items:
        scene_id = item["scene_id"]
        estimated = float(item.get("estimated_credits") or 0.0)

        over, reason = hf.over_budget(project, estimated)
        if over:
            st.error(f"{scene_id}: {reason}")
            _log(project, "higgsfield", False, f"{scene_id} 예산 초과로 중단")
            continue
        if int(item.get("attempts", 0)) >= max_attempts:
            st.warning(f"{scene_id}: 최대 시도 횟수({max_attempts}회)에 도달했습니다. "
                       "수동 업로드를 사용하세요.")
            continue

        need, reason = hf.needs_approval(project, estimated)
        if need and scene_id not in approved and force_scene != scene_id:
            approved.add(scene_id)
            st.session_state[approved_key] = approved
            st.warning(f"{scene_id}: {reason}\n\n승인하려면 이 장면의 '승인 후 생성'을 한 번 더 누르세요.")
            continue

        request = _build_request(project, item)
        if not request.prompt.strip():
            st.error(f"{scene_id}: 프롬프트가 비어 있습니다.")
            continue

        placeholder = st.empty()
        lines: list[str] = []

        def on_line(text: str, _ph=placeholder, _lines=lines) -> None:
            _lines.append(text)
            _ph.code("\n".join(_lines[-8:]), language="text")

        with st.spinner(f"{scene_id} 생성 중... (최대 {settings.get('timeout_seconds', 900)}초)"):
            try:
                result = hf.generate_clip(project, request, on_line=on_line)
            except (hf.HiggsfieldUnavailable, hf.HiggsfieldUnsupported) as exc:
                placeholder.empty()
                st.error(f"{scene_id}: {exc}\n\n수동 업로드로 진행하세요.")
                _log(project, "higgsfield", False, f"{scene_id}: {exc}")
                item["status"] = "ai_failed"
                pm.save_project(project)
                continue
        placeholder.empty()

        item["attempts"] = int(item.get("attempts", 0)) + 1
        hf.record_generation(project, request, result, item["attempts"], estimated)
        if result.ok:
            sp.apply_generated(plan_items, scene_id, result.file, True)
            _register_cut_for_scene(project, item, result.file)
            _log(project, "higgsfield", True, f"{scene_id} 생성 성공 ({result.credits:.1f} 크레딧)")
            st.success(f"{scene_id}: {result.message}")
        else:
            item["status"] = "ai_failed"
            _log(project, "higgsfield", False, f"{scene_id} 생성 실패: {result.message[:200]}")
            st.error(f"{scene_id} 생성 실패: {result.message}")
            st.caption("이 장면만 수동 업로드로 대체하거나, 재시도할 수 있습니다.")
        pm.save_project(project)

    st.session_state[approved_key] = set()
