#!/usr/bin/env python3
"""설치용 ZIP을 만든다: dist/book-publishing.zip

Claude 앱(claude.ai, 데스크톱)과 ChatGPT는 'SKILL.md가 든 폴더 하나'를 ZIP으로 올려 설치한다.
이 도구는 book-publishing/ 폴더를 그 형식으로 묶는다. 같은 내용이면 같은 ZIP이 나오도록
파일 순서와 시각을 고정한다 (tests/test_consistency.py가 ZIP이 최신인지 확인한다).

사용법: python3 tools/package.py
"""
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = ROOT / "book-publishing"
OUT = ROOT / "dist" / "book-publishing.zip"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints"}
SKIP_SUFFIX = {".pyc", ".pyo", ".zip", ".log", ".tmp", ".swp"}


def files():
    """ZIP에 넣을 파일. 숨김 파일, 캐시, 압축 파일, 임시 파일은 뺀다."""
    out = []
    for f in sorted(SKILL.rglob("*")):
        parts = f.relative_to(SKILL).parts
        if not f.is_file() or set(parts) & SKIP_DIRS or any(x.startswith(".") for x in parts) or f.suffix in SKIP_SUFFIX:
            continue
        out.append(f)
    return out


def build(out=OUT):
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            info = zipfile.ZipInfo(f"book-publishing/{f.relative_to(SKILL).as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if f.suffix == ".py" else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    size = out.stat().st_size
    if size > 50 * 1024 * 1024:
        out.unlink()  # 한도를 넘는 ZIP을 남겨 두면 그대로 배포될 수 있다
        sys.exit(f"ZIP이 50MB를 넘습니다 ({size:,}바이트). 앱 업로드 한도를 넘어 지웠습니다.")
    return out


if __name__ == "__main__":
    path = build()
    print(f"{path.relative_to(ROOT)} ({path.stat().st_size:,}바이트, 파일 {len(files())}개)")
