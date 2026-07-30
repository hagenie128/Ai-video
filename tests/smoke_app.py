"""Streamlit 앱 실행 스모크 테스트: 실제로 서버를 띄우고 모든 화면을 렌더해 예외를 잡는다.

실행: python tests/smoke_app.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8599


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.port", str(PORT), "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    ok = False
    body = ""
    try:
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        if ok:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        time.sleep(3)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    print(f"[{'PASS' if ok else 'FAIL'}] Streamlit 서버 기동 (healthz)")
    print(f"[{'PASS' if 'streamlit' in body.lower() else 'FAIL'}] 메인 페이지 응답")

    traces = re.findall(r"Traceback \(most recent call last\):(?:.|\n)*?(?=\n\S|\Z)", out or "")
    errors = [line for line in (out or "").splitlines()
              if re.search(r"(ModuleNotFoundError|ImportError|SyntaxError|NameError|AttributeError)", line)]
    if traces or errors:
        print("[FAIL] 서버 로그에 오류 발견")
        print((out or "")[-4000:])
        return 1
    print("[PASS] 서버 로그에 파이썬 오류 없음")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
