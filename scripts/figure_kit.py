#!/usr/bin/env python3
"""책 그림을 같은 스타일로 만드는 도구. 책마다 쓰는 figures.py가 이 모듈을 불러 쓴다.

책마다 달라지는 것은 '무엇을 그릴지'뿐이다. 글꼴, 색, 크기, 해상도, 파일명은 여기서 고정한다.
색은 user-book-toc.md "## 출판 설정"의 주 색상, 보조 색상을 따른다.

figures.py 예시 (output/{책}/figures.py). 스킬 경로를 적지 않는다. 실행은
`python3 "$SKILL_DIR/scripts/figure_kit.py" run output/{책}/figures.py`로 하고, 그러면 `fk`가 미리 준비되고
그림은 figures.py와 같은 폴더의 images/에 저장된다 (대화창이 바뀌어 스킬 위치가 달라져도 그대로 돈다).

    fk.flow(1, ["자료 올리기", "질문하기", "노트로 저장"])
    fk.compare(2, "사용 전", ["보고서 2시간 읽기"], "사용 후", ["5분 요약 확인"])
    fk.bars(3, ["무료", "Plus", "Pro"], [50, 300, 600], unit="개")
    fk.timeline(4, [("1주차", "개인 사용"), ("2주차", "팀 공유")])
    fk.cards(5, [("소스", "올린 자료"), ("노트", "저장한 답"), ("채팅", "질문 창")])

데모: python3 figure_kit.py demo <출력 폴더>
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

KOREAN_FONTS = ["Pretendard", "Apple SD Gothic Neo", "AppleGothic", "Malgun Gothic", "NanumGothic",
                "Noto Sans CJK KR", "Noto Sans KR", "Source Han Sans K"]
STYLE = {"primary": "#1F4E79", "secondary": "#2E75B6", "light": "#E8F0FE", "gray": "#666666",
         "font": None, "size": 14, "out": pathlib.Path("images"), "dpi": 200}


def setup(toc_path=None, out_dir=None):
    """글꼴과 색을 준비한다. out_dir를 생략하면 현재 폴더의 images/에 저장한다."""
    toc_path = pathlib.Path(toc_path) if toc_path else C.find_toc()
    if toc_path and toc_path.is_file():
        vals, _ = C.parse_kv(C.get_section(toc_path.read_text(encoding="utf-8"), "출판 설정"),
                             ["주 색상", "보조 색상", "박스 배경색", "캡션 색상", "본문 폰트"])
        if vals.get("본문 폰트"):
            KOREAN_FONTS.insert(0, vals["본문 폰트"])  # 책 본문과 같은 글꼴을 먼저 찾는다
        STYLE["primary"] = vals.get("주 색상") or STYLE["primary"]
        STYLE["secondary"] = vals.get("보조 색상") or STYLE["secondary"]
        STYLE["light"] = vals.get("박스 배경색") or STYLE["light"]
        STYLE["gray"] = vals.get("캡션 색상") or STYLE["gray"]
    names = {f.name for f in font_manager.fontManager.ttflist}
    STYLE["font"] = next((f for f in KOREAN_FONTS if f in names), None)
    if STYLE["font"]:
        plt.rcParams["font.family"] = STYLE["font"]
    else:
        print("경고: 한글 글꼴을 찾지 못했습니다. 그림의 한글이 깨질 수 있습니다.", file=sys.stderr)
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = STYLE["size"]
    if out_dir:
        STYLE["out"] = pathlib.Path(out_dir)
    STYLE["out"].mkdir(parents=True, exist_ok=True)
    return STYLE


def save(fig, num):
    """fig{NN}.png로 저장하고 경로를 돌려준다. 번호는 원고의 [그림 N]과 같아야 한다.

    matplotlib으로 직접 그린 그림도 이 함수로 저장한다. 그래야 그림 속 글자와 수치가 사실 확인 표와 검사기에 들어간다.
    """
    path = STYLE["out"] / f"fig{int(num):02d}.png"
    fig.savefig(path, dpi=STYLE["dpi"], bbox_inches="tight", facecolor="white")
    _record(fig, num, path)
    plt.close(fig)
    return path


def _record(fig, num, path):
    """그림 속 글자(제목, 라벨, 상자 글, 눈금, 표 칸)와 막대·선의 값을 images/figdata.json에 적는다.

    그림 파일의 해시도 함께 적는다. 그림을 다른 방법으로 덮어쓰면 해시가 달라져 기록을 믿지 않는다.
    """
    import json
    from matplotlib.text import Text
    texts = []
    for t in fig.findobj(Text):
        s = " ".join(t.get_text().split())
        if s and t.get_visible() and s not in texts:
            texts.append(s)
    values = []
    for ax in fig.axes:
        for c in ax.containers:
            values += [float(v) for v in getattr(c, "datavalues", [])]
        if ax.axison:  # 장식용 선(흐름도·타임라인 캔버스)은 빼고, 축이 있는 그래프의 선만 값으로 본다
            for line in ax.get_lines():
                ys = list(line.get_ydata())
                if len(ys) <= 60:
                    values += [float(y) for y in ys]
    values = [int(v) if v == int(v) else round(v, 4) for v in values]
    cells = []
    for ax in fig.axes:
        for tbl in getattr(ax, "tables", []):
            for cell in tbl.get_celld().values():
                s = " ".join(cell.get_text().get_text().split())
                if s and s not in texts and s not in cells:
                    cells.append(s)
    rec_path = STYLE["out"] / "figdata.json"
    try:
        data = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else {}
    except ValueError:
        data = {}
    has_image = any(ax.images for ax in fig.axes)  # 캡처 화면처럼 붙여 넣은 이미지 속 글자는 기록할 수 없다
    data[f"fig{int(num):02d}"] = {"texts": texts, "cells": cells, "values": values, "sha": C.sha(pathlib.Path(path)), "has_image": has_image}
    rec_path.write_text(json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _wrap(text, width_in, size):
    """상자 폭에 맞춰 줄을 나눈다. 한글 한 글자는 글자 크기의 약 1배 폭이다."""
    import textwrap
    per_line = max(4, int(width_in * 72 / (size or STYLE["size"]) * 0.95))
    return "\n".join(textwrap.wrap(str(text), per_line, break_long_words=True)) or str(text)


def _box(ax, x, y, w, h, text, fill, color="white", size=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                linewidth=0, facecolor=fill))
    sz = size or STYLE["size"]
    wrapped = _wrap(text, w, sz)
    lines = wrapped.count("\n") + 1
    if lines * sz * 1.3 / 72 > h:  # 줄이 상자 높이를 넘으면 글자를 줄인다
        sz = max(9, sz * h * 72 / (lines * sz * 1.3))
        wrapped = _wrap(text, w, sz)
    ax.text(x + w / 2, y + h / 2, wrapped, ha="center", va="center", color=color, fontsize=sz)


def _canvas(w, h):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")
    return fig, ax


def flow(num, steps):
    """왼쪽에서 오른쪽으로 이어지는 단계 흐름도."""
    if not steps:
        raise ValueError(f"fig{int(num):02d} flow: 단계가 하나 이상 있어야 합니다")
    n = len(steps)
    w = max(8, 2.4 * n)
    fig, ax = _canvas(w, 2.4)
    bw = (w - 0.6 * (n - 1) - 0.4) / n
    for i, s in enumerate(steps):
        x = 0.2 + i * (bw + 0.6)
        _box(ax, x, 0.6, bw, 1.2, s, STYLE["primary"] if i % 2 == 0 else STYLE["secondary"])
        if i < n - 1:
            ax.annotate("", xy=(x + bw + 0.5, 1.2), xytext=(x + bw + 0.1, 1.2),
                        arrowprops=dict(arrowstyle="-|>", color=STYLE["gray"], lw=2))
    return save(fig, num)


def compare(num, left_title, left_items, right_title, right_items):
    """두 열 비교 (예: 사용 전/후, A 도구/B 도구)."""
    rows = max(len(left_items), len(right_items))
    h = 1.6 + 0.8 * rows
    fig, ax = _canvas(9, h)
    for col, (title, items, color) in enumerate([(left_title, left_items, STYLE["gray"]),
                                                 (right_title, right_items, STYLE["primary"])]):
        x = 0.3 + col * 4.5
        _box(ax, x, h - 1.1, 4.1, 0.8, title, color)
        for i, it in enumerate(items):
            _box(ax, x, h - 1.9 - i * 0.8, 4.1, 0.65, it, STYLE["light"], color="#222222", size=STYLE["size"] - 1)
    return save(fig, num)


def bars(num, labels, values, unit=""):
    """가로 막대 비교. 값 옆에 숫자를 붙인다."""
    if not labels or len(labels) != len(values):
        raise ValueError(f"fig{int(num):02d} bars: 라벨({len(labels)}개)과 값({len(values)}개)의 개수가 같아야 합니다")
    fig, ax = plt.subplots(figsize=(8, 0.7 * len(labels) + 1.2))
    ys = range(len(labels))[::-1]
    ax.barh(list(ys), values, color=[STYLE["primary"]] + [STYLE["secondary"]] * (len(values) - 1), height=0.55)
    ax.set_yticks(list(ys))
    ax.set_yticklabels(labels)
    for y, v in zip(ys, values):
        shown = f"{v:,}" if float(v) == int(v) else f"{v:,.2f}".rstrip("0").rstrip(".")
        ax.text(v, y, f"  {shown}{unit}", va="center", color="#222222")
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_xticks([])
    lo, hi = min(0, min(values)), max(0, max(values))
    ax.set_xlim(lo * 1.25, (hi * 1.25) or 1)  # 음수 값도 축이 뒤집히지 않게
    return save(fig, num)


def timeline(num, events):
    """[(시점, 설명), ...] 가로 타임라인."""
    if not events:
        raise ValueError(f"fig{int(num):02d} timeline: 사건이 하나 이상 있어야 합니다")
    events = [(e, "") if isinstance(e, str) else (tuple(e) + ("",))[:2] for e in events]
    n = len(events)
    w = max(8, 2.2 * n)
    fig, ax = _canvas(w, 2.8)
    ax.plot([0.3, w - 0.3], [1.6, 1.6], color=STYLE["gray"], lw=2)
    slot = (w - 0.6) / n
    for i, (when, what) in enumerate(events):
        x = 0.3 + slot * (i + 0.5)
        ax.plot(x, 1.6, "o", color=STYLE["primary"], ms=14)
        ax.text(x, 2.2, _wrap(when, slot * 0.9, STYLE["size"]), ha="center", va="bottom", color=STYLE["primary"], fontweight="bold")
        ax.text(x, 1.15, _wrap(what, slot * 0.9, STYLE["size"] - 1), ha="center", va="top", color="#222222", fontsize=STYLE["size"] - 1)
    return save(fig, num)


def cards(num, items, cols=3):
    """[(제목, 설명), ...] 카드 격자. 개념 정리, 구성 요소 소개에 쓴다."""
    if not items or int(cols) < 1:
        raise ValueError(f"fig{int(num):02d} cards: 카드가 하나 이상, 열(cols)이 1 이상이어야 합니다")
    items = [(it, "") if isinstance(it, str) else (tuple(it) + ("",))[:2] for it in items]
    rows = (len(items) + cols - 1) // cols
    fig, ax = _canvas(3.2 * cols, 2.0 * rows)
    for i, (title, desc) in enumerate(items):
        r, c = divmod(i, cols)
        x, y = 0.15 + c * 3.2, 2.0 * (rows - r - 1) + 0.15
        _box(ax, x, y, 2.9, 1.7, "", STYLE["light"])
        # 제목과 설명이 카드 높이(1.7)에 들어가도록 글자 크기를 줄여 가며 맞춘다
        ts, ds = STYLE["size"], STYLE["size"] - 2
        for _ in range(8):
            tw, dw = _wrap(title, 2.6, ts), _wrap(desc, 2.6, ds)
            need = (tw.count("\n") + 1) * ts * 1.3 / 72 + (dw.count("\n") + 1) * ds * 1.3 / 72 + 0.25
            if need <= 1.55 or ds <= 8:
                break
            ts, ds = max(9, ts - 1), max(8, ds - 1)
        top = y + 1.6
        ax.text(x + 1.45, top, tw, ha="center", va="top", color=STYLE["primary"], fontweight="bold", fontsize=ts)
        ax.text(x + 1.45, top - (tw.count("\n") + 1) * ts * 1.3 / 72 - 0.12, dw, ha="center", va="top", color="#222222", fontsize=ds)
        if need > 1.55:
            print(f"경고: fig{int(num):02d} 카드 '{str(title)[:10]}…'의 글이 길어 카드를 넘칩니다. 글을 줄이세요.", file=sys.stderr)
    return save(fig, num)


def run(script):
    """figures.py를 실행한다. fk(이 모듈)를 미리 넣어 두고, 그림은 그 파일 옆 images/에 저장한다."""
    script = pathlib.Path(script).resolve()
    setup(out_dir=script.parent / "images")
    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
    exec(code, {"fk": sys.modules[__name__], "__name__": "__main__", "__file__": str(script)})
    made = sorted(p.name for p in (script.parent / "images").glob("fig*.png"))
    print(f"그림 {len(made)}개: {', '.join(made[:10])}{' …' if len(made) > 10 else ''}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "run" and len(sys.argv) != 3:
        sys.exit("사용법: python3 figure_kit.py run <figures.py>")
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        if not pathlib.Path(sys.argv[2]).is_file():
            sys.exit(f"그림 스크립트가 없습니다: {sys.argv[2]}")
        try:
            run(sys.argv[2])
        except (ValueError, TypeError, ZeroDivisionError) as e:
            sys.exit(f"그림을 만들지 못했습니다: {e}")
    elif len(sys.argv) == 3 and sys.argv[1] == "demo":
        setup(out_dir=sys.argv[2])
        print(flow(1, ["자료 올리기", "질문하기", "노트 저장", "팀 공유"]))
        print(compare(2, "사용 전", ["보고서 2시간 읽기", "회의록 직접 작성"], "사용 후", ["5분 요약 확인", "액션 아이템 자동 추출"]))
        print(bars(3, ["무료", "Plus", "Pro"], [50, 300, 600], unit="개"))
        print(timeline(4, [("1주차", "개인 사용"), ("2주차", "팀 공유"), ("4주차", "부서 확산")]))
        print(cards(5, [("소스", "올린 자료"), ("노트", "저장한 답"), ("채팅", "질문하는 창")]))
    else:
        print(__doc__)
