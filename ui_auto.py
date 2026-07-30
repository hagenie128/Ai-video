"""'0. AI 자동 제작' 화면.

STEP 1 범위: 브리프 입력 · 자료 업로드 · 대본 자동 생성/편집.
이후 STEP 에서 장면 계획, AI 영상, 자동 타임라인, 전체 파이프라인이 이 화면에 붙는다.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

import ai_provider
import config
import project_manager as pm
import script_generator as sg
import tts_service
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
    from app import add_media_files
    return add_media_files(project, files, kind)


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


def _media_summary(project: dict) -> str:
    analysis = project.get("media_analysis") or {}
    if analysis:
        lines = []
        for rel, info in list(analysis.items())[:20]:
            tags = ", ".join(info.get("tags", [])[:5])
            lines.append(f"- {Path(rel).name}: {info.get('description') or info.get('kind', '')} [{tags}]")
        return "\n".join(lines)
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

def page(project: dict, bump) -> None:
    st.header("0. AI 자동 제작")
    st.caption(SAFETY_NOTE)
    brief = _brief(project)

    section_brief(project, brief)
    st.divider()
    section_material(project, brief, bump)
    st.divider()
    section_script(project, brief)

    project["ai_settings"] = config.sanitize_for_project(config.load_settings())
    pm.save_project(project)
