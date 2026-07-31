"""'0. AI 자동 제작' 화면.

STEP 1 범위: 브리프 입력 · 자료 업로드 · 대본 자동 생성/편집.
이후 STEP 에서 장면 계획, AI 영상, 자동 타임라인, 전체 파이프라인이 이 화면에 붙는다.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

import ai_provider
import automation_pipeline as ap
import config
import higgsfield_service as hf
import media_analyzer as ma
import media_manager
import project_manager as pm
import renderer
import scene_planner as sp
import script_generator as sg
import timeline as tl
import tts_service
import ui_higgsfield
from utils import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

SAFETY_NOTE = (
    "업로드 자료의 사용 권한은 사용자 책임입니다 · AI 생성 결과는 세부 형상과 글자가 달라질 수 있습니다 · "
    "사실성 콘텐츠는 제공 자료에 근거해 직접 검수하세요 · 근거 없는 수치는 자동 생성하지 않습니다."
)


def _brief(project: dict) -> dict:
    brief = project.get("ai_brief") or {}
    merged = {**sg.empty_brief(), **brief}
    merged["project_name"] = merged.get("project_name") or project.get("name", "")
    project["ai_brief"] = merged
    return merged


# ---------------------------------------------------------------- 브리프 입력

def section_brief(project: dict, brief: dict) -> None:
    st.subheader("① 기본 정보")
    c1, c2, c3 = st.columns(3)
    brief["project_name"] = c1.text_input("프로젝트명", value=brief.get("project_name", ""), key="ab_name")
    brief["category"] = c2.text_input("업종 / 카테고리", value=brief.get("category", ""),
                                      key="ab_cat", placeholder="예: 수제 가죽 지갑")
    brief["product_name"] = c3.text_input("상품명", value=brief.get("product_name", ""), key="ab_prod")

    brief["description"] = st.text_area("상품 설명", value=brief.get("description", ""), height=90,
                                        key="ab_desc", placeholder="제품이 무엇이고 어떻게 만들었는지")
    c1, c2 = st.columns(2)
    brief["benefits"] = c1.text_area("핵심 장점 (줄바꿈으로 여러 개)", value=brief.get("benefits", ""),
                                     height=110, key="ab_ben")
    brief["facts"] = c2.text_area("강조할 사실 (수치는 여기에만)", value=brief.get("facts", ""),
                                  height=110, key="ab_fact",
                                  placeholder="예: 가죽 두께 1.4mm\n제작 기간 5일")
    st.caption("대본에 들어가는 구체적 수치는 '강조할 사실'과 '상품 설명'에 적힌 값만 사용합니다.")

    c1, c2, c3 = st.columns(3)
    brief["target"] = c1.text_input("타깃 고객", value=brief.get("target", ""), key="ab_target")
    brief["mood"] = c2.text_input("원하는 분위기", value=brief.get("mood", ""), key="ab_mood",
                                  placeholder="예: 차분하고 담백한")
    brief["forbidden"] = c3.text_area("금지 표현 (줄바꿈)", value=brief.get("forbidden", ""),
                                      height=80, key="ab_forbid")

    c1, c2, c3, c4 = st.columns(4)
    brief["brand_visible"] = c1.checkbox("브랜드명 노출", value=bool(brief.get("brand_visible", False)),
                                         key="ab_brandvis")
    brief["brand_name"] = c2.text_input("브랜드명", value=brief.get("brand_name", ""), key="ab_brand")
    brief["target_length"] = c3.selectbox(
        "목표 길이", sg.TARGET_LENGTHS,
        index=sg.TARGET_LENGTHS.index(int(brief.get("target_length", 30)))
        if int(brief.get("target_length", 30)) in sg.TARGET_LENGTHS else 2,
        format_func=lambda v: f"{v}초", key="ab_len",
    )
    brief["platform"] = c4.selectbox(
        "플랫폼", sg.PLATFORMS,
        index=sg.PLATFORMS.index(brief.get("platform", "youtube_shorts"))
        if brief.get("platform") in sg.PLATFORMS else 0,
        format_func=lambda v: sg.PLATFORM_LABELS[v], key="ab_platform",
    )

    c1, c2 = st.columns([1, 2])
    brief["content_type"] = c1.selectbox(
        "콘텐츠 유형", sg.CONTENT_TYPES,
        index=sg.CONTENT_TYPES.index(brief.get("content_type", "product_intro"))
        if brief.get("content_type") in sg.CONTENT_TYPES else 2,
        format_func=lambda v: sg.CONTENT_TYPE_LABELS[v], key="ab_ctype",
    )
    if brief["content_type"] == "custom":
        brief["custom_content_type"] = c2.text_input(
            "직접 입력한 유형", value=brief.get("custom_content_type", ""), key="ab_ctype_custom")


def section_material(project: dict, brief: dict, bump) -> None:
    st.subheader("② 자료")
    c1, c2 = st.columns(2)
    with c1:
        photos = st.file_uploader(
            "상품 사진 여러 장", type=[e.lstrip(".") for e in sorted(IMAGE_EXTS)],
            accept_multiple_files=True, key=f"auto_img_{bump()}")
        if photos and st.button(f"사진 {len(photos)}장 등록", type="primary", key="auto_add_img"):
            added = _register_media(project, photos, "image")
            pm.save_project(project)
            st.toast(f"사진 {added}장 등록")
            st.rerun()
    with c2:
        videos = st.file_uploader(
            "기존 영상 여러 개", type=[e.lstrip(".") for e in sorted(VIDEO_EXTS)],
            accept_multiple_files=True, key=f"auto_vid_{bump()}")
        if videos and st.button(f"영상 {len(videos)}개 등록", type="primary", key="auto_add_vid"):
            added = _register_media(project, videos, "video")
            pm.save_project(project)
            st.toast(f"영상 {added}개 등록")
            st.rerun()

    cuts = project.get("cuts", [])
    images = sum(1 for c in cuts if c.get("type") == "image")
    clips = sum(1 for c in cuts if c.get("type") == "video")
    st.caption(f"현재 등록된 자료: 사진 {images}장 · 영상 {clips}개")

    with st.expander("선택 입력 (참고 대본 / 링크 / 로고 / 금지 이미지)"):
        brief["reference_script"] = st.text_area("참고 대본", value=brief.get("reference_script", ""),
                                                height=90, key="ab_ref_script")
        brief["reference_links"] = st.text_area("참고 링크 (줄바꿈)", value=brief.get("reference_links", ""),
                                               height=70, key="ab_ref_links")
        c1, c2 = st.columns(2)
        with c1:
            logo = st.file_uploader("브랜드 로고", type=[e.lstrip(".") for e in sorted(IMAGE_EXTS)],
                                    key=f"auto_logo_{bump()}")
            if logo is not None and st.button("로고 등록", key="auto_logo_btn"):
                rel = pm.save_upload(project, "images", f"logo_{logo.name}", logo.getvalue())
                brief["logo_file"] = rel
                pm.save_project(project)
                st.success("로고를 등록했습니다. (AI 영상 프롬프트에는 로고 생성 금지가 기본입니다)")
            if brief.get("logo_file"):
                st.caption(f"등록된 로고: `{brief['logo_file']}`")
        with c2:
            banned = st.file_uploader("금지 이미지 (사용하지 않을 사진)",
                                      type=[e.lstrip(".") for e in sorted(IMAGE_EXTS)],
                                      accept_multiple_files=True, key=f"auto_ban_{bump()}")
            if banned and st.button(f"금지 이미지 {len(banned)}장 등록", key="auto_ban_btn"):
                names = brief.setdefault("banned_images", [])
                for f in banned:
                    rel = pm.save_upload(project, "images", f"banned_{f.name}", f.getvalue())
                    names.append(rel)
                    for cut in project.get("cuts", []):
                        if cut.get("file") == rel:
                            cut["enabled"] = False
                pm.save_project(project)
                st.success(f"{len(banned)}장을 금지 목록에 넣었습니다. 자동 배치에서 제외됩니다.")
            if brief.get("banned_images"):
                st.caption(f"금지 이미지 {len(brief['banned_images'])}장")

    with st.expander("음성 / BGM / 효과음 (선택)"):
        c1, c2 = st.columns(2)
        with c1:
            voice = st.file_uploader("음성 직접 업로드 (TTS 대신 사용)",
                                     type=[e.lstrip(".") for e in sorted(AUDIO_EXTS)],
                                     key=f"auto_voice_{bump()}")
            if voice is not None and st.button("음성 등록", key="auto_voice_btn"):
                from utils import audio_duration
                rel = pm.save_upload(project, "voice", voice.name, voice.getvalue())
                try:
                    duration = audio_duration(pm.abs_path(project, rel))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"음성 길이 감지 실패: {exc}")
                    duration = 0.0
                project["voice"] = {"file": rel, "duration": duration, "name": voice.name}
                project["tts"] = {"provider": "upload", "file": rel, "duration": duration, "segments": []}
                pm.save_project(project)
                st.success(f"음성 등록 완료 ({duration:.2f}초)")
                st.rerun()
            if project.get("voice"):
                st.caption(f"등록된 음성: {project['voice'].get('name')} · "
                           f"{project['voice'].get('duration', 0):.2f}초")
        with c2:
            bgm = st.file_uploader("BGM", type=[e.lstrip(".") for e in sorted(AUDIO_EXTS)],
                                   key=f"auto_bgm_{bump()}")
            if bgm is not None and st.button("BGM 등록", key="auto_bgm_btn"):
                rel = pm.save_upload(project, "bgm", bgm.name, bgm.getvalue())
                project["bgm"] = {"file": rel, "name": bgm.name}
                pm.save_project(project)
                st.success("BGM 등록 완료")
                st.rerun()
            if project.get("bgm"):
                st.caption(f"등록된 BGM: {project['bgm'].get('name')}")


def _register_media(project: dict, files, kind: str) -> int:
    """미디어 업로드 → 컷 등록. 기존 '3. 미디어 업로드' 와 같은 규칙을 쓴다."""
    return media_manager.add_media_files(project, files, kind)


# ---------------------------------------------------------------- 대본

def section_script(project: dict, brief: dict) -> None:
    st.subheader("③ 대본 자동 생성")
    st.caption(f"LLM 상태: {ai_provider.status_text()} · TTS 상태: {tts_service.status_text()}")

    media_summary = _media_summary(project)
    c1, c2 = st.columns([1, 2])
    if c1.button("대본 생성 / 다시 생성", type="primary", key="auto_gen_script"):
        with st.spinner("대본 생성 중..."):
            images = _sample_images(project, 6)
            script = sg.generate(brief, media_summary, images)
        project["script_data"] = script
        project["script"] = script.get("full_script", "")
        _log(project, "script", True, sg.summary_text(script))
        pm.save_project(project)
        if script.get("fallback_reason"):
            st.warning(script["fallback_reason"])
        st.rerun()
    c2.caption("LLM 키가 없으면 입력한 자료를 재구성하는 규칙 기반 대본이 만들어집니다 (Plan B). "
               "생성 후 모든 문장을 직접 수정할 수 있습니다.")

    script = project.get("script_data") or {}
    if not script.get("scenes"):
        st.info("아직 대본이 없습니다. 위 버튼으로 생성하세요.")
        return

    st.success(sg.summary_text(script))
    if script.get("fallback_reason"):
        st.caption(f"※ {script['fallback_reason']}")

    c1, c2, c3 = st.columns(3)
    c1.metric("제목", script.get("title", "")[:20] or "-")
    c2.metric("톤", script.get("tone", "")[:16] or "-")
    c3.metric("예상 길이", f"{script.get('estimated_duration', 0):.1f}초")
    st.caption(f"컨셉: {script.get('concept', '-')}")

    tab_edit, tab_scene, tab_check = st.tabs(["전체 대본", "장면별 편집", "점검"])

    with tab_edit:
        text = st.text_area("전체 대본", value=script.get("full_script", ""), height=200, key="auto_full")
        c1, c2, c3, c4, c5 = st.columns(5)
        if c1.button("장면에 재배분", key="auto_redistribute"):
            _, message = sg.rebuild_from_full_script(script, brief, text)
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.success(message)
            st.rerun()
        if c2.button("후킹만 재생성", key="auto_rehook"):
            with st.spinner("후킹 재생성 중..."):
                _, message = sg.regenerate_hook(script, brief, variant=int(st.session_state.get("_hookv", 1)))
            st.session_state["_hookv"] = int(st.session_state.get("_hookv", 1)) + 1
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.info(message)
            st.rerun()
        if c3.button("길이 줄이기", key="auto_shorter"):
            _, message = sg.resize(script, brief, "shorter")
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.info(message)
            st.rerun()
        if c4.button("길이 늘리기", key="auto_longer"):
            _, message = sg.resize(script, brief, "longer")
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.info(message)
            st.rerun()
        tone = c5.text_input("말투 변경", value="", key="auto_tone", placeholder="예: 더 담백하게")
        if c5.button("말투 적용", key="auto_tone_btn", disabled=not tone.strip()):
            with st.spinner("말투 변경 중..."):
                _, message = sg.change_tone(script, brief, tone.strip())
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.info(message)
            st.rerun()

    with tab_scene:
        st.caption("장면별로 나레이션·자막·영상 종류를 직접 수정할 수 있습니다. 수정 후 아래 '장면 저장'을 누르세요.")
        for i, scene in enumerate(script["scenes"]):
            with st.container(border=True):
                head, body = st.columns([1, 5])
                with head:
                    st.markdown(f"**{scene['scene_id']}**")
                    st.caption(scene.get("role", ""))
                    st.caption(f"{scene.get('start', 0):.1f}–{scene.get('end', 0):.1f}초")
                    if scene.get("visual_type") == "higgsfield_video":
                        st.caption("🤖 AI 영상")
                with body:
                    r1, r2 = st.columns([3, 2])
                    scene["voiceover"] = r1.text_input("나레이션", value=scene.get("voiceover", ""),
                                                       key=f"sc_v_{i}")
                    scene["subtitle"] = r2.text_input("자막", value=scene.get("subtitle", ""),
                                                      key=f"sc_s_{i}")
                    r1, r2, r3 = st.columns([1.2, 1.2, 2])
                    scene["role"] = r1.selectbox(
                        "역할", sg.SCENE_ROLES,
                        index=sg.SCENE_ROLES.index(scene.get("role", "explanation"))
                        if scene.get("role") in sg.SCENE_ROLES else 4, key=f"sc_r_{i}")
                    scene["visual_type"] = r2.selectbox(
                        "영상 종류", sg.VISUAL_TYPES,
                        index=sg.VISUAL_TYPES.index(scene.get("visual_type", "uploaded_image"))
                        if scene.get("visual_type") in sg.VISUAL_TYPES else 0, key=f"sc_t_{i}")
                    scene["visual_description"] = r3.text_input(
                        "화면 설명", value=scene.get("visual_description", ""), key=f"sc_d_{i}")
                    if scene.get("visual_type") == "higgsfield_video":
                        scene["higgsfield_prompt"] = st.text_area(
                            "Higgsfield 프롬프트", value=scene.get("higgsfield_prompt", ""),
                            height=70, key=f"sc_p_{i}")
        if st.button("장면 저장", type="primary", key="auto_save_scenes"):
            sg.assign_timings(script["scenes"], int(brief.get("target_length") or 30))
            script["full_script"] = " ".join(s["voiceover"] for s in script["scenes"] if s.get("voiceover"))
            script["estimated_duration"] = round(script["scenes"][-1]["end"], 2)
            project["script"] = script["full_script"]
            pm.save_project(project)
            st.success("저장했습니다.")
            st.rerun()

    with tab_check:
        c1, c2 = st.columns(2)
        if c1.button("사실성 점검", key="auto_factcheck"):
            issues = sg.check_facts(script, brief)
            if issues:
                st.warning("확인이 필요한 항목:\n\n" + "\n".join(f"· {i}" for i in issues))
            else:
                st.success("제공 자료에 없는 수치나 단정 표현이 발견되지 않았습니다.")
        if c2.button("금지 표현 점검", key="auto_forbidcheck"):
            hits = sg.check_forbidden(script, brief)
            if hits:
                st.error("금지 표현이 남아 있습니다:\n\n" + "\n".join(f"· {h}" for h in hits))
            else:
                st.success("금지 표현이 없습니다.")
        st.caption("점검은 자동 판정이 아니라 확인 목록입니다. 최종 판단은 사용자가 합니다.")


# ---------------------------------------------------------------- 자료 분석 / 장면 계획

def section_analysis(project: dict) -> None:
    st.subheader("④ 자료 분석과 역할 지정")
    if not project.get("cuts"):
        st.info("먼저 사진이나 영상을 등록하세요.")
        return

    c1, c2 = st.columns([1, 3])
    use_vision = c2.checkbox(
        "비전 모델로 사진 내용까지 분석 (LLM 키 필요)", value=ai_provider.available(),
        key="auto_vision", disabled=not ai_provider.available(),
        help="키가 없으면 해상도·비율·밝기·파일명·유사도 기준으로 분석합니다.")
    if c1.button("자료 분석 실행", type="primary", key="auto_analyze"):
        bar = st.progress(0.0, text="분석 준비")

        def cb(fraction: float, message: str) -> None:
            bar.progress(min(1.0, fraction), text=message)

        try:
            ma.analyze_project(project, use_vision=use_vision, progress_cb=cb)
            _log(project, "analyze", True, ma.stats(project))
        except Exception as exc:  # noqa: BLE001 - 분석 실패로 앱을 멈추지 않는다
            _log(project, "analyze", False, str(exc))
            st.error(f"분석 실패: {exc}")
        bar.empty()
        pm.save_project(project)
        st.rerun()

    analysis = project.get("media_analysis") or {}
    if not analysis:
        st.caption("분석 전입니다. 분석하면 각 자료에 역할과 태그, 관심 영역이 자동으로 지정됩니다.")
        return

    st.success(ma.stats(project))
    broken = [rel for rel, info in analysis.items() if info.get("error")]
    if broken:
        st.warning(f"읽을 수 없는 파일 {len(broken)}개는 자동 배치에서 제외됩니다: "
                   + ", ".join(Path(b).name for b in broken[:5]))

    with st.expander("자료별 역할 / 태그 / 고정", expanded=False):
        for cut in project.get("cuts", []):
            info = analysis.get(cut["file"]) or {}
            with st.container(border=True):
                thumb, body = st.columns([1, 6])
                with thumb:
                    path = pm.abs_path(project, cut["file"])
                    if cut["type"] == "image" and path.is_file():
                        st.image(str(path), width="stretch")
                    else:
                        st.markdown("### 🎞️" if cut["type"] == "video" else "### ⚠️")
                with body:
                    st.caption(f"**{cut.get('name')}** · 품질 {info.get('quality', 0):.2f} · "
                               f"유사그룹 {info.get('similar_group', -1)}"
                               + (f" · {info['description']}" if info.get("description") else ""))
                    r1, r2, r3 = st.columns([1.2, 3, 1])
                    roles = ma.ROLES
                    current = cut.get("scene_role") or "unused"
                    cut["scene_role"] = r1.selectbox(
                        "역할", roles, index=roles.index(current) if current in roles else 7,
                        format_func=lambda v: ma.ROLE_LABELS[v], key=f"an_role_{cut['id']}")
                    cut["tags"] = r2.multiselect(
                        "태그", ma.TAGS, default=[t for t in (cut.get("tags") or []) if t in ma.TAGS],
                        key=f"an_tags_{cut['id']}")
                    cut["locked"] = r3.checkbox("고정", value=bool(cut.get("locked")),
                                                key=f"an_lock_{cut['id']}",
                                                help="자동 재구성에서도 이 자료의 배치/효과를 유지합니다.")
        if st.button("역할·태그 저장", key="auto_save_roles"):
            pm.save_project(project)
            st.success("저장했습니다.")


def section_plan(project: dict, brief: dict) -> None:
    st.subheader("⑤ 장면 계획 (사진 vs AI 영상)")
    script = project.get("script_data") or {}
    if not script.get("scenes"):
        st.info("먼저 대본을 생성하세요.")
        return

    settings = config.load_settings()["higgsfield"]
    c1, c2 = st.columns([1, 3])
    max_clips = c2.slider("AI 영상 최대 개수", 0, 6, int(settings.get("max_clips", 3)), 1,
                          key="auto_maxclips",
                          help="실제 사진으로 표현하기 어려운 장면만 AI로 만듭니다. 예산을 넘으면 자동으로 줄어듭니다.")
    if c1.button("장면 계획 만들기", type="primary", key="auto_plan"):
        with st.spinner("장면 계획 중..."):
            plan_items = sp.plan(project, script, max_ai_clips=max_clips)
        project["scene_plan"] = plan_items
        _log(project, "scene_plan", bool(plan_items), sp.summary(project, plan_items))
        pm.save_project(project)
        st.rerun()

    plan_items = project.get("scene_plan") or []
    if not plan_items:
        st.caption("계획 전입니다.")
        return

    st.success(sp.summary(project, plan_items))
    budget = hf.budget_state(project)
    c1, c2, c3 = st.columns(3)
    c1.metric("예상 크레딧", f"{sp.total_estimated_credits(plan_items):.1f}")
    c2.metric("이번 달 사용", f"{budget['used']:.1f}")
    c3.metric("월 예산", "무제한" if budget["monthly_budget"] <= 0 else f"{budget['monthly_budget']:.0f}")

    rows = []
    for item in plan_items:
        rows.append({
            "장면": item["scene_id"],
            "역할": item["role"],
            "자막": item["subtitle"].replace("\n", " "),
            "길이": f"{item['duration']:.2f}s",
            "화면": {"uploaded_image": "사진", "uploaded_video": "기존 영상",
                    "higgsfield_video": "AI 영상", "black_screen": "블랙"}.get(item["visual_type"],
                                                                            item["visual_type"]),
            "자료": Path(item["media_file"]).name if item["media_file"] else "-",
            "상태": {
                "matched": "매칭", "reused": "재사용", "ai_pending": "AI 생성 대기",
                "ai_ready": "AI 완료", "ai_failed": "AI 실패", "photo_fallback": "사진 대체",
                "unmatched": "미배정",
            }.get(item["status"], item["status"]),
            "크레딧": f"{item['estimated_credits']:.1f}" if item["needs_ai"] else "",
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    unused = sp.unused_media(project, plan_items)
    if unused:
        st.caption(f"미사용 자료 {len(unused)}개 (삭제되지 않고 타임라인에 '사용 안 함'으로 남습니다): "
                   + ", ".join(Path(u).name for u in unused[:8]))

    ai_list = sp.ai_items(plan_items)
    if ai_list:
        with st.expander(f"AI 영상 프롬프트 {len(ai_list)}개 (직접 수정 가능)", expanded=False):
            for item in ai_list:
                st.markdown(f"**{item['scene_id']}** · {item['role']} · 모델 `{item['model']}` · "
                            f"{item['ai_seconds']}초 · 예상 {item['estimated_credits']:.1f} 크레딧"
                            + (f" · 참고 이미지 `{Path(item['reference_image']).name}`"
                               if item.get("reference_image") else ""))
                item["higgsfield_prompt"] = st.text_area(
                    "프롬프트", value=item["higgsfield_prompt"], height=110,
                    key=f"pl_prompt_{item['scene_id']}", label_visibility="collapsed")
            if st.button("프롬프트 저장", key="auto_save_prompts"):
                pm.save_project(project)
                st.success("저장했습니다.")
        st.info("AI 영상은 실제 생성 전까지 참고 사진이 임시로 배치됩니다. "
                "생성은 'Higgsfield 영상' 단계에서 승인 후 진행합니다.")


def section_draft(project: dict) -> None:
    st.subheader("⑥ 쇼츠 초안 자동 구성")
    plan_items = project.get("scene_plan") or []
    if not plan_items:
        st.info("먼저 장면 계획을 만드세요.")
        return

    tts = project.get("tts") or None
    c1, c2 = st.columns([1, 3])
    if c1.button("쇼츠 초안 자동 구성", type="primary", key="auto_draft_btn"):
        count, message = tl.auto_build(project, plan_items, tts)
        _log(project, "timeline", bool(count), message)
        pm.save_project(project)
        (st.success if count else st.warning)(message)
        if count:
            st.rerun()
    c2.caption("첫 컷은 hook, 첫 3초 안에 최소 3컷, 사진/영상 교차, 하드컷 중심으로 배치합니다. "
               "음성이 있으면 장면별 실제 발화 길이를 컷 길이로 씁니다. "
               "쓰지 않은 자료는 삭제하지 않고 '사용 안 함'으로 남습니다.")

    if not project.get("cuts"):
        return
    stats = tl.stats(project)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("총 길이", f"{stats['duration']:.2f}초")
    c2.metric("첫 3초 컷", f"{stats['first_window']}컷")
    c3.metric("사진 / 영상", f"{stats['images']} / {stats['videos']}")
    c4.metric("미사용", f"{stats['unused']}개")
    if stats["first_window"] < 3:
        st.warning("첫 3초 컷이 3개보다 적습니다. 앞쪽 컷 길이를 줄이거나 컷을 추가하세요.")
    st.caption("세부 조정은 '4. 타임라인' 화면에서 할 수 있습니다.")


def _media_summary(project: dict) -> str:
    if project.get("media_analysis"):
        return ma.summary_for_prompt(project)
    cuts = project.get("cuts", [])
    if not cuts:
        return "업로드된 자료 없음"
    images = [c for c in cuts if c.get("type") == "image"]
    videos = [c for c in cuts if c.get("type") == "video"]
    lines = [f"사진 {len(images)}장, 영상 {len(videos)}개 (아직 상세 분석 전)"]
    lines += [f"- {c.get('name')}" for c in cuts[:15]]
    return "\n".join(lines)


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


def _log(project: dict, step: str, ok: bool, message: str) -> None:
    from datetime import datetime
    project.setdefault("automation_log", []).append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "step": step, "ok": bool(ok), "message": message[:500],
    })


# ---------------------------------------------------------------- 화면

# ---------------------------------------------------------------- 전체 자동 생성

def _options(project: dict) -> dict:
    saved = project.get("automation_options") or {}
    return {**ap.default_options(), **saved}


def section_pipeline(project: dict, brief: dict) -> None:
    st.subheader("⑧ AI 쇼츠 전체 생성")
    opts = _options(project)

    st.caption("생성 옵션")
    c1, c2, c3, c4 = st.columns(4)
    opts["script"] = c1.checkbox("대본 자동 생성", value=bool(opts["script"]), key="op_script")
    opts["tts"] = c2.checkbox("TTS 자동 생성", value=bool(opts["tts"]), key="op_tts")
    opts["higgsfield"] = c3.checkbox("Higgsfield 영상 자동 생성", value=bool(opts["higgsfield"]),
                                     key="op_hf",
                                     help="크레딧이 사용됩니다. 승인 방식과 예산은 '8. AI 연동 설정'에서 정합니다.")
    opts["subtitles"] = c4.checkbox("자동 자막", value=bool(opts["subtitles"]), key="op_sub")
    c1, c2, c3, c4 = st.columns(4)
    opts["bgm"] = c1.checkbox("BGM 자동 선택", value=bool(opts["bgm"]), key="op_bgm")
    opts["sfx"] = c2.checkbox("효과음 자동 선택", value=bool(opts["sfx"]), key="op_sfx")
    opts["timeline"] = c3.checkbox("자동 타임라인 구성", value=bool(opts["timeline"]), key="op_tl")
    opts["render"] = c4.checkbox("최종 MP4 자동 렌더링", value=bool(opts["render"]), key="op_render")
    c1, c2 = st.columns(2)
    opts["vision"] = c1.checkbox("사진 비전 분석 사용", value=bool(opts["vision"]) and ai_provider.available(),
                                 key="op_vision", disabled=not ai_provider.available())
    preview_only = c2.checkbox("빠른 저화질로 먼저 확인 (540x960)", value=False, key="op_preview")
    project["automation_options"] = opts

    if opts["higgsfield"]:
        st.warning("Higgsfield 자동 생성이 켜져 있습니다. 승인 방식이 '매번 승인'이면 "
                   "파이프라인은 생성을 건너뛰고 승인 대기로 표시합니다.")

    c1, c2 = st.columns([1, 3])
    start = c1.button("🚀 AI 쇼츠 전체 생성", type="primary", key="op_run",
                      disabled=not project.get("cuts"))
    if not project.get("cuts"):
        c2.caption("먼저 사진이나 영상을 등록하세요.")
    else:
        c2.caption("9단계를 순서대로 진행합니다. 한 단계가 실패해도 멈추지 않고 가능한 단계까지 계속 진행하며, "
                   "실패한 단계는 수동 작업으로 전환됩니다.")

    if start:
        _run_pipeline(project, opts, preview_only)

    _render_pipeline_report(project, opts, preview_only)


def _run_pipeline(project: dict, opts: dict, preview: bool, start_at: str = "") -> None:
    total = len(ap.STEPS)
    bar = st.progress(0.0, text="준비 중")
    status = st.empty()

    def progress(index: int, label: str, message: str) -> None:
        bar.progress(min(1.0, (index + 0.5) / total), text=f"{index + 1}/{total} {label}")
        status.caption(f"{index + 1}/{total} {label} — {message}")

    if start_at:
        result = ap.retry_from(project, start_at, opts, progress, preview)
    else:
        result = ap.run(project, opts, progress, preview)
    bar.progress(1.0, text="완료")
    status.empty()

    st.session_state["_pipeline"] = {
        "steps": [(s.key, s.label, s.status, s.message, s.detail) for s in result.steps],
        "output": str(result.output) if result.output else "",
        "manual": result.manual_actions,
        "summary": result.summary(),
    }
    pm.save_project(project)
    st.rerun()


def _render_pipeline_report(project: dict, opts: dict, preview: bool) -> None:
    data = st.session_state.get("_pipeline")
    if not data:
        return

    st.divider()
    st.markdown(f"**진행 결과** — {data['summary']}")
    for i, (key, label, step_status, message, detail) in enumerate(data["steps"]):
        icon = {"ok": "✅", "failed": "❌", "manual": "✋", "skipped": "⏭"}.get(step_status, "•")
        cols = st.columns([4.2, 1.0])
        cols[0].markdown(f"{icon} **{i + 1}/{len(data['steps'])} {label}** — {message}")
        if detail:
            cols[0].caption(detail)
        if step_status in ("failed", "manual"):
            if cols[1].button("이 단계부터 재시도", key=f"retry_{key}"):
                _run_pipeline(project, opts, preview, start_at=key)

    if data["manual"]:
        st.warning("수동으로 해야 할 일:\n\n" + "\n".join(f"· {m}" for m in data["manual"]))

    output = data.get("output")
    if output and Path(output).is_file():
        st.success(f"결과 파일: `{output}`")
        st.video(output)
        st.download_button("MP4 다운로드", Path(output).read_bytes(), Path(output).name,
                           "video/mp4", key="op_dl")
        st.caption("장면·대본·자막을 수정한 뒤 '7. 출력'에서 다시 렌더링하면 반영됩니다.")


# ---------------------------------------------------------------- 자막 미리보기

def section_subtitle_preview(project: dict) -> None:
    st.subheader("⑨ 자막 미리보기 (9:16 안전 영역)")
    cfg = project.setdefault("subtitle", {})
    cues = project.get("auto_cues") or []
    default_text = ""
    if cues:
        default_text = str(cues[0].get("text") or "")
    elif project.get("script_data", {}).get("scenes"):
        default_text = project["script_data"]["scenes"][0].get("subtitle", "")

    c1, c2 = st.columns([2, 3])
    with c1:
        text = st.text_input("미리볼 자막", value=default_text.replace("\n", " "), key="sp_text")
        cfg["margin_v"] = st.slider("자막 위치 (하단 여백)", 60, 900, int(cfg.get("margin_v", 300)), 10,
                                    key="sp_margin")
        cfg["font_size"] = st.slider("글자 크기", 30, 140, int(cfg.get("font_size", 74)), 1, key="sp_size")
        cfg["max_chars"] = st.slider("한 줄 최대 글자 수", 6, 24, int(cfg.get("max_chars", 14)), 1,
                                     key="sp_chars")
        show_safe = st.checkbox("모바일 안전 영역 표시", value=True, key="sp_safe")
        if st.button("미리보기 만들기", type="primary", key="sp_make"):
            try:
                path = renderer.preview_frame(project, text, safe_area=show_safe)
                st.session_state["_sub_preview"] = str(path)
                pm.save_project(project)
            except Exception as exc:  # noqa: BLE001 - 미리보기 실패가 작업을 막지 않는다
                st.error(f"미리보기 생성 실패: {exc}")
        st.caption("붉은 영역은 쇼츠 UI(제목·버튼)가 가릴 수 있는 자리입니다. 자막이 그 안에 들어가지 않게 하세요.")
    with c2:
        preview = st.session_state.get("_sub_preview")
        if preview and Path(preview).is_file():
            st.image(preview, width=320)
        else:
            st.caption("왼쪽에서 '미리보기 만들기'를 누르면 실제 렌더링과 같은 자막 모양을 확인할 수 있습니다.")

    if cues:
        with st.expander(f"자동 자막 {len(cues)}개 확인/수정"):
            for i, cue in enumerate(cues[:40]):
                c1, c2, c3 = st.columns([1, 1, 4])
                c1.caption(f"{cue['start']:.2f}s")
                c2.caption(f"{cue['end']:.2f}s")
                cue["text"] = c3.text_input(f"자막 {i + 1}", value=cue.get("text", ""),
                                            key=f"sc_cue_{i}", label_visibility="collapsed")
            if st.button("자동 자막 저장", key="sp_save_cues"):
                pm.save_project(project)
                st.success("저장했습니다. 다시 렌더링하면 반영됩니다.")


def page(project: dict, bump) -> None:
    st.header("0. AI 자동 제작")
    st.caption(SAFETY_NOTE)
    brief = _brief(project)

    section_brief(project, brief)
    st.divider()
    section_material(project, brief, bump)
    st.divider()
    section_pipeline(project, brief)
    st.divider()
    section_script(project, brief)
    st.divider()
    section_analysis(project)
    st.divider()
    section_plan(project, brief)
    st.divider()
    ui_higgsfield.section(project)
    st.divider()
    section_draft(project)
    st.divider()
    section_subtitle_preview(project)

    project["ai_settings"] = config.sanitize_for_project(config.load_settings())
    pm.save_project(project)
