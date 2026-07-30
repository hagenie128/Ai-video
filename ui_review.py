"""'9. 결과 검수' 와 '10. 사용량 통계' 화면."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

import higgsfield_service as hf
import project_manager as pm
import quality_checker as qc
import renderer
import timeline as tl
from renderer import RenderError
from utils import OUTPUTS_DIR, find_ffmpeg


def _show_checks(checks: list) -> None:
    level = qc.worst(checks)
    box = {qc.PASS: st.success, qc.WARN: st.warning, qc.FAIL: st.error}[level]
    box(qc.summary(checks))
    fails = [c for c in checks if c.level == qc.FAIL]
    warns = [c for c in checks if c.level == qc.WARN]
    passes = [c for c in checks if c.level == qc.PASS]

    for group, title, expanded in ((fails, "실패", True), (warns, "경고", True), (passes, "통과", False)):
        if not group:
            continue
        with st.expander(f"{title} {len(group)}건", expanded=expanded):
            for check in group:
                st.markdown(f"{check.icon} **{check.name}** — {check.message}")
                if check.hint:
                    st.caption(f"↳ {check.hint}")


def page_review(project: dict) -> None:
    st.header("9. 결과 검수")

    if not find_ffmpeg():
        st.error("FFmpeg 가 없어 검수와 렌더링을 할 수 없습니다.")
        return

    st.subheader("렌더 전 자동 점검")
    c1, c2 = st.columns([1, 3])
    if c1.button("점검 실행", type="primary", key="qc_run"):
        with st.spinner("점검 중..."):
            checks = qc.check_project(project)
        st.session_state["_qc_pre"] = [(c.name, c.level, c.message, c.hint) for c in checks]
    c2.caption("타임라인·자막·오디오·자료 상태를 확인합니다. 자동으로 고치지 않고 확인 목록만 보여줍니다.")

    pre = st.session_state.get("_qc_pre")
    if pre:
        checks = [qc.Check(*row) for row in pre]
        _show_checks(checks)
        blocked = qc.worst(checks) == qc.FAIL
    else:
        blocked = False
        st.caption("아직 점검하지 않았습니다.")

    st.divider()
    st.subheader("최종 렌더링과 결과 검사")
    c1, c2, c3 = st.columns(3)
    if c1.button("미리보기 렌더 + 검사", key="qc_render_preview"):
        _render_and_check(project, preview=True)
    if c2.button("최종 렌더 + 검사", type="primary", key="qc_render_final",
                 disabled=blocked):
        _render_and_check(project, preview=False)
    if blocked:
        c3.caption("실패 항목이 있어 최종 렌더 버튼이 잠겼습니다. 항목을 고치고 다시 점검하세요.")
    else:
        last = project.get("last_output")
        if last and Path(last).is_file() and c3.button("마지막 결과 다시 검사", key="qc_recheck"):
            _check_output(project, Path(last))

    data = st.session_state.get("_qc_post")
    if data:
        st.divider()
        st.subheader("결과 검사")
        checks = [qc.Check(*row) for row in data["checks"]]
        _show_checks(checks)

        frames = [(label, Path(p)) for label, p in data["frames"] if Path(p).is_file()]
        if frames:
            st.caption("주요 시점 프레임")
            cols = st.columns(len(frames))
            for col, (label, path) in zip(cols, frames):
                col.image(str(path), caption=label, width="stretch")

        output = Path(data["output"])
        if output.is_file():
            st.video(str(output))
            c1, c2 = st.columns(2)
            c1.download_button("MP4 다운로드", output.read_bytes(), output.name, "video/mp4",
                               key="qc_dl", width="stretch")
            if c2.button("재수정 (타임라인으로 이동)", key="qc_fix", width="stretch"):
                st.session_state.setdefault("_s", {})["page"] = "4. 타임라인"
                st.rerun()
            st.caption(f"저장 위치: `{output}`")

    st.divider()
    st.caption(f"출력 폴더: `{OUTPUTS_DIR}`")
    logs = sorted((pm.project_dir(project) / "logs").glob("render_*.log"), reverse=True)
    if logs:
        with st.expander(f"최근 렌더링 로그 ({len(logs)}개)"):
            st.code(logs[0].read_text(encoding="utf-8")[-6000:], language="text")

    steps = project.get("automation_log") or []
    if steps:
        with st.expander(f"자동화 진행 기록 ({len(steps)}건)"):
            for entry in reversed(steps[-40:]):
                icon = "✅" if entry.get("ok") else "⚠️"
                st.caption(f"{icon} [{entry.get('time', '')}] {entry.get('step')} — {entry.get('message')}")


def _render_and_check(project: dict, preview: bool) -> None:
    bar = st.progress(0.0, text="준비 중")
    status = st.empty()

    def cb(fraction: float, message: str) -> None:
        bar.progress(min(1.0, fraction), text=f"{message} ({fraction * 100:.0f}%)")

    try:
        output = renderer.render(project, preview=preview, progress_cb=cb)
    except RenderError as exc:
        bar.empty()
        status.empty()
        st.error(f"렌더링 실패: {exc}")
        if exc.detail:
            st.code(exc.detail, language="text")
        if exc.log_path:
            st.caption(f"로그: `{exc.log_path}`")
        return
    except RuntimeError as exc:
        bar.empty()
        st.error(str(exc))
        return
    bar.progress(1.0, text="완료")
    status.empty()
    project["last_output"] = str(output)
    pm.save_project(project)
    _check_output(project, output)


def _check_output(project: dict, output: Path) -> None:
    with st.spinner("결과 검사 중..."):
        checks, frames = qc.check_output(project, output)
    st.session_state["_qc_post"] = {
        "checks": [(c.name, c.level, c.message, c.hint) for c in checks],
        "frames": [(label, str(path)) for label, path in frames],
        "output": str(output),
    }
    st.rerun()


# ---------------------------------------------------------------- 사용량 통계

def page_stats(project: dict | None) -> None:
    st.header("10. 사용량 통계")
    st.caption("모든 프로젝트의 project.json 을 읽어 월별로 집계합니다. 인증값은 저장되지 않습니다.")

    projects: list[dict] = []
    broken: list[str] = []
    for name in pm.list_projects():
        try:
            projects.append(pm.load_project(name))
        except (OSError, ValueError, KeyError) as exc:
            broken.append(f"{name}: {exc}")

    if broken:
        st.warning("읽을 수 없는 프로젝트: " + ", ".join(broken[:5]))
    if not projects:
        st.info("아직 프로젝트가 없습니다.")
        return

    finished = [p for p in projects if p.get("last_output") and Path(p["last_output"]).is_file()]
    stats = hf.generation_stats(projects)
    total_generations = sum(v["generations"] for v in stats.values())
    total_credits = sum(v["credits"] for v in stats.values())
    total_failed = sum(v["failed"] for v in stats.values())
    total_retries = sum(v["retries"] for v in stats.values())
    success = sum(v["success"] for v in stats.values())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("프로젝트 수", len(projects))
    c2.metric("완성 영상 수", len(finished))
    c3.metric("AI 생성 횟수", total_generations)
    c4.metric("사용 크레딧", f"{total_credits:.1f}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("영상당 평균 크레딧", f"{(total_credits / success):.1f}" if success else "0.0")
    c2.metric("생성 실패 수", total_failed)
    c3.metric("재생성 수", total_retries)
    budget = hf.budget_state(project) if project else None
    c4.metric("이번 달 사용", f"{budget['used']:.1f}" if budget else "-")

    st.divider()
    st.subheader("월별 상세")
    if not stats:
        st.caption("Higgsfield 생성 기록이 없습니다.")
    else:
        rows = []
        for month, value in stats.items():
            rows.append({
                "월": month,
                "프로젝트": value["projects"],
                "생성 횟수": value["generations"],
                "성공": value["success"],
                "실패": value["failed"],
                "재생성": value["retries"],
                "사용 크레딧": f"{value['credits']:.1f}",
                "영상당 평균": f"{value['avg_credits']:.1f}",
            })
        st.dataframe(rows, width="stretch", hide_index=True)

    st.divider()
    st.subheader("프로젝트별")
    rows = []
    for p in projects:
        usage = p.get("credit_usage") or {}
        records = p.get("higgsfield_generations") or []
        rows.append({
            "프로젝트": p.get("name"),
            "수정 시각": p.get("updated_at", ""),
            "컷": len(tl.enabled_cuts(p)),
            "길이(초)": f"{tl.total_duration(p):.1f}",
            "AI 생성": len(records),
            "실패": sum(1 for r in records if r.get("status") != "success"),
            "크레딧(실제)": f"{float(usage.get('actual') or 0):.1f}",
            "완성 영상": "있음" if p.get("last_output") and Path(p["last_output"]).is_file() else "없음",
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    if project:
        records = project.get("higgsfield_generations") or []
        with st.expander(f"현재 프로젝트 생성 기록 ({len(records)}건)"):
            if not records:
                st.caption("기록이 없습니다.")
            else:
                for record in reversed(records[-30:]):
                    icon = "✅" if record.get("status") == "success" else "❌"
                    st.markdown(
                        f"{icon} `{record.get('date')}` · **{record.get('scene_id')}** · "
                        f"{record.get('model')} · {record.get('duration')}초 · "
                        f"예상 {record.get('estimated_credits')} / 실제 {record.get('actual_credits')} 크레딧 · "
                        f"시도 {record.get('attempt')}회")
                    if record.get("job_id"):
                        st.caption(f"작업 ID: {record['job_id']} · 파일: {record.get('file') or '없음'}")
                    if record.get("status") != "success" and record.get("message"):
                        st.caption(f"↳ {record['message'][:200]}")
                st.download_button(
                    "생성 기록 JSON 다운로드",
                    json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"),
                    f"{pm.safe_filename(project['name'])}_higgsfield.json", "application/json",
                    key="stats_dl")

    log_path = hf.LOG_PATH
    if log_path.is_file():
        with st.expander("Higgsfield 실행 로그 (민감값 마스킹됨)"):
            st.code(log_path.read_text(encoding="utf-8")[-6000:], language="text")
