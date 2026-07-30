# STEP 0 기준 동작 기록 (baseline)

측정 환경: Python 3.11 / FFmpeg 6.1.1 / streamlit 1.60

`python tests/test_baseline_render.py` 결과 — 전 항목 통과

| 항목 | 결과 |
|---|---|
| 프로젝트 생성/저장 (`projects/<name>/project.json`) | PASS |
| 사진 9장 + 영상 2개 컷 등록 | PASS |
| 목표 길이 맞추기 (`timeline.fit_to_target`) | PASS · 12.00초 |
| SRT / ASS 생성 | PASS |
| 미리보기 렌더링 (540x960) | PASS |
| 최종 렌더링 | PASS |
| 해상도 1080x1920 | PASS |
| 오디오 스트림(AAC) 존재 | PASS |
| 길이 오차 < 0.4초 | PASS · 실제 12.00s / 예상 12.00s |
| 프로젝트 재불러오기 | PASS |

이 기준선은 이후 모든 STEP 이후에도 동일하게 통과해야 한다.
