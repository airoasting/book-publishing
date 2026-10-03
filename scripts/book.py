#!/usr/bin/env python3
"""책 작업 상태 관리 도구. 모든 단계는 이 도구로 '지금 어디인가'를 판정한다.

LLM이 표를 읽고 추측하던 일(이어하기 지점, 점수 계산, 원고 병합, 변경 로그 확인,
단계 순서, 리뷰가 지금 원고 기준인지)을 코드로 옮겨, 같은 폴더 상태면 언제나
같은 다음 행동이 나오게 한다.

명령 (작업 폴더에서 실행):
  status [--json]            지금 단계, 진행률, 다음 행동, 입력 파일
  init [--date YYYYMMDD]     새 책 폴더(draft/YYYYMMDD[_NN]) 생성 후 활성화
  use <폴더명>               다른 책으로 활성 전환
  doctor                     책 설정, 검증 규칙, 출판 설정, 의존성 점검
  config 키=값 ...           작업 설정 저장 (chunk_unit=part|two_parts, auto_continue=true|false)
  approve <대상> [--note]    사람 확인 기록: outline, tone, facts, low-score
  complete <ID>              산출물 끝에 완료 마커 추가 (01, 02, 원고 조각 _pNN)
  score <리뷰ID>             리뷰 지적에 ID를 매기고 점수 표와 마커를 붙인다
  merge <원고ID>             원고 조각을 부 이름 기준으로 합친다 (변경 로그 게이트, 규칙 검사 포함)
  split <ID>                 원고를 부 단위 읽기용 조각으로 나눈다 (_read/ 폴더)
  next-round                 합평 2라운드 미통과 시 3라운드 준비
  reset <ID>                 그 단계와 이후 단계를 백업하고 지운다 (되돌리기)
  accept-toc                 시작 뒤 바뀐 책 설정을 이 책의 기준으로 받아들인다
  figures <ID>               원고의 [그림 N: …] 목록 출력 (figures.py 작성용)
  facts                      출판 전 사실 확인 표(output/{책}/fact-check.md)를 만든다 (사람 확인 3)
  export [--to 폴더]         작업 묶음(user_input/, draft/{책}, output/{책})을 ZIP 하나로 내보낸다
  import <ZIP>               내보낸 작업 묶음을 작업 폴더에 풀어 이어서 작업한다

Claude 앱(claude.ai)과 ChatGPT는 대화창마다 파일 공간이 새로 시작될 수 있다. 세션을 끝낼 때
export로 묶어 사용자에게 내려받게 하고, 다음 대화에서 그 ZIP을 올리면 import로 이어 간다.
"""
import argparse
import contextlib
import datetime
import io
import json
import pathlib
import re
import shutil
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

ROOT = pathlib.Path.cwd()
DRAFT = ROOT / "draft"
OUTPUT = ROOT / "output"
BOOK_DIR_RE = re.compile(r"^\d{8}(_\d{2})?$")

# 단계 정의. 파이프라인의 단일 정본이다. 문서의 단계 표는 이 표를 따른다.
# kind: research | outline | draft | review | ensemble | revise
# after: 먼저 끝나 있어야 하는 단계 (생략하면 바로 앞 단계)
STAGES = [
    {"id": "01_research-notes", "kind": "research", "phase": 1, "doc": "references/research.md", "title": "자료 조사", "after": []},
    {"id": "02_outline", "kind": "outline", "phase": 2, "doc": "references/write.md 1단계", "title": "구조 설계", "inputs": ["01_research-notes"]},
    {"id": "03_draft-v1", "kind": "draft", "phase": 2, "doc": "references/write.md 2단계", "title": "초안", "inputs": ["02_outline"]},
    {"id": "04_review-red", "kind": "review", "phase": 3, "doc": "references/review.md 1단계", "title": "비평가 리뷰", "inputs": ["03_draft-v1"]},
    {"id": "05_draft-v2", "kind": "revise", "phase": 3, "doc": "references/review.md 수정 단계", "title": "비평 반영", "base": "03_draft-v1", "reviews": ["04_review-red"]},
    {"id": "06_review-pink", "kind": "review", "phase": 3, "doc": "references/review.md 3단계", "title": "독자 리뷰", "inputs": ["05_draft-v2"]},
    {"id": "07_draft-v3", "kind": "revise", "phase": 3, "doc": "references/review.md 수정 단계", "title": "독자 리뷰 반영", "base": "05_draft-v2", "reviews": ["06_review-pink"]},
    {"id": "08_ensemble-review-1", "kind": "ensemble", "phase": 3, "doc": "references/review.md 5단계", "title": "합평 1라운드", "inputs": ["07_draft-v3"]},
    {"id": "09_draft-v4", "kind": "revise", "phase": 3, "doc": "references/review.md 수정 단계", "title": "합평 반영", "base": "07_draft-v3", "reviews": ["08_ensemble-review-1"]},
    {"id": "10_ensemble-review-2", "kind": "ensemble", "phase": 3, "doc": "references/review.md 6단계", "title": "합평 {round}라운드 (게이트)", "inputs": ["09_draft-v4"]},
    {"id": "11_draft-final", "kind": "revise", "phase": 3, "doc": "references/review.md 수정 단계", "title": "합평 통과본", "base": "09_draft-v4", "reviews": ["10_ensemble-review-2"]},
    {"id": "12_review-editor", "kind": "review", "phase": 3, "doc": "references/review.md 7단계", "title": "편집자 리뷰", "inputs": ["11_draft-final"]},
    {"id": "13_review-marketer", "kind": "review", "phase": 3, "doc": "references/review.md 8단계", "title": "마케터 리뷰 (참고용)", "inputs": ["11_draft-final"], "after": ["11_draft-final"]},
    {"id": "14_review-proofreader", "kind": "review", "phase": 3, "doc": "references/review.md 8단계", "title": "교정 리뷰", "inputs": ["11_draft-final"], "after": ["11_draft-final"]},
    {"id": "15_manuscript", "kind": "revise", "phase": 3, "doc": "references/review.md 9단계", "title": "최종 원고", "base": "11_draft-final", "reviews": ["12_review-editor", "14_review-proofreader"],
     "after": ["12_review-editor", "13_review-marketer", "14_review-proofreader"]},
]
STAGE_BY_ID = {s["id"]: s for s in STAGES}
PHASES = {1: "리서치", 2: "집필", 3: "리뷰", 4: "출판"}
PUBLISH_STEPS = ["verify", "docx", "metadata"]
TOTAL_STEPS = len(STAGES) + len(PUBLISH_STEPS)
SEV = "🔴🟡🟢"
# 지적 줄: 목록·번호·인용·헤딩·굵게 접두를 허용하고, 대괄호는 있어도 없어도 된다
FINDING_LINE_RE = re.compile(r"^[ \t]*(?:(?:[-*+>•]|\d+[.)])[ \t]+|#{1,6}[ \t]+)*(?:\*\*|__)?(?:\[?(🔴|🟡|🟢)\]?|\[(필수|권장|참고)\])(?:\*\*|__)?")
SEV_WORD = {"필수": "🔴", "권장": "🟡", "참고": "🟢"}
# 지적 형식은 아니지만 심각도를 말하는 줄 ('- 필수: …', '(심각도 높음) …', 표 안의 '[필수]')
_STRONG = (r"(?:필수|권장|심각한|심각도?|중요도|중대|치명적?|긴급|고위험|위험도|우선\s?순위|큰\s?문제|Severity|Priority|Critical|Blocker|"
           r"Must(?:\s?fix)?|MUST|Urgent|P[0-3])")
_WEAK = r"(?:높음|중간|낮음|상|중|하|High|Medium|Low|Major|Minor|경고|레드)"
SEV_TEXT_RE = re.compile(
    r"^[ \t]*(?:[-*+>]|\d+[.)]|#{1,6})?[ \t]*(?:\*\*|__)?[(\[]?" + _STRONG
    + r"(?:\s?(?:문제|이슈|사항|오류|항목))?(?:\*\*|__)?\s*(?:[:)\]—–=]|\s-\s)"                      # '- 긴급:', '**치명적 문제**:', '1. **Critical** —'
    r"|^[ \t]*(?:[-*+>]|\d+[.)])?[ \t]*(?:\*\*|__)?[(\[]?" + _WEAK + r"(?:\*\*|__)?\s*[:)\]]"            # '- High:', '- 높음:'
    r"|[(\[]\s*(?:심각도|중요도|위험도|우선\s?순위|Severity|Priority)\s*[:：=]"                                # '(심각도: 높음)'
    r"|^[ \t]*(?:[-*+>]|\d+[.)])?[ \t]*(?:심각도|중요도|위험도|Severity|Priority)\s*[:：=]"                 # '- 심각도 = 높음'
    r"|\[\s*(?:필수|권장|참고|높음|중간|낮음|중대|MUST|Critical|High|Major|Minor|Blocker|Medium|Low|HIGH[^\]]*|CRITICAL[^\]]*)\s*\]"
    r"|\(\s*(?:높음|낮음|중간|치명|High|Critical)\s*\)"
    r"|^\s*\|.*\|\s*(?:\*\*)?(?:높음|중간|낮음|상|중|하|High|Medium|Low|Critical|필수|권장|중대)(?:\*\*)?\s*(?:\([^)]*\))?\s*\|",  # 표의 심각도 칸
    re.I,
)
SEV_SYMBOL_RE = re.compile(r"[🟠⚠🔺❗‼🚨⛔❌🛑🔥🟥🔶🟧⭐★]")
WHOLE_BOOK = ("전체", "책전체", "전반", "공통")
FINDING_ID_RE = re.compile(r"R\d\d(?:-\d)?-\d\d")
# 지적 섹션 밖에 둘 수 있는 섹션 (총평, 마케터의 한 줄 소개 후보 등). 그 밖의 섹션은 지적 섹션으로 본다
FREE_SECTIONS = {"총평", "한줄소개후보", "요약", "메모", "참고자료", "점수(book.py계산)"}  # 이름이 정확히 같을 때만 자유 섹션
# 지적 아래 하위 항목으로 허용하는 줄
SUBITEM_NAMES = "원문|문제점|수정 제안|제안|수정안|근거|출처|참고|예시|위치|이유"
SUBITEM_RE = re.compile(r"^\s*[-*•]\s*(?:" + SUBITEM_NAMES.replace(" ", "\\s?") + r")\s*[:：]")
REVIEWER_RE = re.compile(r"^##\s*평가자\s*(\d+)\s*[:.]\s*(.+?)\s*$", re.M)
SCORE_HEAD = "## 점수 (book.py 계산)"


class BookError(Exception):
    pass


# ---------------------------------------------------------------------------
# 활성 책, 상태 파일
# ---------------------------------------------------------------------------
def book_dirs():
    if not DRAFT.is_dir():
        return []
    return sorted(p for p in DRAFT.iterdir() if p.is_dir() and BOOK_DIR_RE.match(p.name))


def active_dir():
    """활성 책. 포인터가 없고 책이 여러 권이면 추측하지 않고 None (status가 고르라고 안내)."""
    ptr = DRAFT / ".active"
    if ptr.is_file():
        name = ptr.read_text(encoding="utf-8").strip()
        if BOOK_DIR_RE.match(name) and (DRAFT / name).is_dir():
            return DRAFT / name
    dirs = book_dirs()
    return dirs[0] if len(dirs) == 1 else None


def require_active():
    d = active_dir()
    if not d:
        dirs = book_dirs()
        if len(dirs) > 1:
            raise BookError(f"책이 여러 권입니다 ({', '.join(p.name for p in dirs)}). 'book.py use <폴더명>'으로 고르세요.")
        raise BookError("활성 책 폴더가 없습니다. 'book.py init'으로 새 책을 시작하세요.")
    return d


def load_state(d):
    p = d / "_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError as e:
        raise BookError(f"{d.name}/_state.json이 깨졌습니다 ({e}). 가장 최근 작업 묶음 ZIP을 다시 불러오거나 _backup/의 사본을 확인하세요.")


def save_state(d, state):
    (d / "_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def stage_file(d, sid):
    return d / f"{sid}.md"


def part_path(d, sid, n):
    return d / f"{sid}_p{n:02d}.md"


def has_marker(path, sid=None):
    if not path.is_file():
        return False
    sid = sid or path.stem
    tail = path.read_text(encoding="utf-8").rstrip().splitlines()[-1:] or [""]
    return tail[0].strip() == f"<!-- STAGE_COMPLETE: {sid} -->"


def load_toc():
    p = C.find_toc(ROOT)
    return (p, p.read_text(encoding="utf-8")) if p else (None, None)


def divisions():
    _, toc = load_toc()
    return [title for title, _ in C.toc_structure(toc)] if toc else []


def label(title):
    return C.despace(C.split_label(title)[0])


def backup(d, path):
    if not path.exists():
        return None
    bdir = d / "_backup"
    bdir.mkdir(exist_ok=True)
    dest = bdir / f"{path.stem}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


def missing_figures(d, ms):
    """원고의 [그림 N] 가운데 output/{책}/images/에 그림 파일이 없는 번호."""
    figs = sorted({int(n) for n in re.findall(r"\[그림\s*(\d+)\s*[:.]", ms.read_text(encoding="utf-8"))})
    img = OUTPUT / d.name / "images"
    return [n for n in figs if not any((img / f"fig{n:02d}{ext}").is_file() for ext in (".png", ".jpg"))]


def evidence_sha(d):
    """출판 승인이 묶이는 근거 자료의 해시: 출처 파일, 그림 스크립트와 그림, 사내·저자 자료."""
    import hashlib
    h = hashlib.sha256()
    out = OUTPUT / d.name
    files = [d / "01_citations.json", out / "figures.py"] + sorted((out / "images").glob("fig*")) \
        + sorted(p for p in (C.input_dir(ROOT) / "sources").rglob("*") if p.is_file() and not p.name.startswith("."))
    for f in files:
        if f.is_file():
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def approval_ok(state, what, path):
    """승인이 있고, 승인한 뒤 대상 파일이 바뀌지 않았으면 True.

    출판 승인(facts, low-score)은 책 설정에도 묶인다. 승인 뒤 'AI 제작 고지'나 검증 규칙을 바꾸면 다시 승인받는다.
    """
    a = state.get("approvals", {}).get(what)
    if not a or a.get("sha") != C.sha(path):
        return False
    if what in ("facts", "low-score"):
        _, toc = load_toc()
        if a.get("toc_sha") != (C.sha(toc) if toc else None):
            return False
        # 근거 자료(출처, 그림, 사내 자료)는 사실 확인 승인에만 묶는다. 낮은 점수 승인은 규칙 점수에 대한 결정이다
        return what != "facts" or a.get("evidence_sha") == evidence_sha(path.parent)
    return True


def tone_part():
    """톤 승인 대상 조각 번호. 장이 있는 첫 부(보통 제1부)다. 프롤로그는 본문보다 사적인 톤이라 대표 목소리로 보지 않는다."""
    _, toc = load_toc()
    struct = C.toc_structure(toc) if toc else []
    return next((i + 1 for i, (_, ch) in enumerate(struct) if ch), 1)


def log_event(state, event, **kw):
    """사후 감사를 위한 기록. 되돌리기나 재채점으로 사라지는 정보도 여기에 남는다."""
    state.setdefault("log", []).append({"at": now(), "event": event, **kw})


def verify_result(d):
    """출판 검사 상태: ('missing'|'red'|'low'|'ok', 점수). 판정 문자열이 아니라 🔴 수와 점수로 판단한다
    (기준 점수를 낮춰 다시 돌려도 결과가 바뀌지 않게)."""
    vr = OUTPUT / d.name / "verify-report.md"
    ms = stage_file(d, "15_manuscript")
    _, toc = load_toc()
    st = load_state(d)
    if report_field(vr, "원고 해시") != C.sha(ms) or (toc and report_field(vr, "책 설정 해시") != C.sha(toc)):
        return "missing", None
    if st.get("toc_sha") and toc and st["toc_sha"] != C.sha(toc):
        return "missing", None  # 검사 규칙이 바뀌었다. accept-toc(기록됨) 후 다시 검사
    red = int(re.sub(r"\D", "", report_field(vr, "🔴 필수") or "0") or 0)
    score = int((report_field(vr, "규칙 준수 점수") or "0").split("/")[0].strip() or 0)
    if red:
        return "red", score
    return ("low" if score < 9 else "ok"), score


def fix_rows(d):
    """_changelog.md '## publish-fix' 표의 내용 행 (칸 3개 이상이 채워진 행만 센다)."""
    log = d / "_changelog.md"
    sec = last_section(log.read_text(encoding="utf-8"), "publish-fix") if log.is_file() else None
    rows = []
    for r in (sec or "").splitlines():
        r = r.strip()
        if not r.startswith("|") or re.match(r"^\|\s*[-:]+", r) or "심각도" in r:
            continue
        if sum(1 for c in r.strip("|").split("|") if len(c.strip()) >= 2 or c.strip() in "✅❌🔴🟡🟢") >= 4:
            rows.append(r)
    return rows


def publish_edit_problem(d, state):
    """병합 뒤 15_manuscript.md를 직접 고쳤다면, 고칠 때마다 '## publish-fix' 표에 새 행이 있어야 한다. 문제면 문자열."""
    rec = state.get("stages", {}).get("15_manuscript", {})
    cur = C.sha(stage_file(d, "15_manuscript"))
    if not rec.get("output_sha") or cur in rec.get("accepted_shas", [rec["output_sha"]]):
        return None
    if len(fix_rows(d)) <= rec.get("fix_rows_seen", 0):
        return ("15_manuscript.md가 병합(또는 마지막 승인) 뒤 직접 수정되었는데 _changelog.md '## publish-fix' 표에 새 행이 없습니다. "
                "무엇을 왜 고쳤는지 '| 항목 | 심각도 | 출처 | 반영 | 사유 |' 표에 한 행 이상 남기세요 (병합 기준본: _15_manuscript.merged.md).")
    return None


def accept_publish_edits(d, state):
    rec = state.get("stages", {}).get("15_manuscript")
    if rec is not None:
        cur = C.sha(stage_file(d, "15_manuscript"))
        rec.setdefault("accepted_shas", [rec.get("output_sha")])
        if cur not in rec["accepted_shas"]:
            rec["accepted_shas"].append(cur)
        rec["fix_rows_seen"] = len(fix_rows(d))


def publish_gate(d, state, need_facts=True):
    """출판(Word 변환) 가능 여부의 단일 판정. status, approve, build_docx가 모두 이 함수만 쓴다.

    반환: None(통과) 또는 (action, 이유). 보고서 텍스트를 믿지 않고 원고를 다시 검사한다.
    """
    for st in STAGES:
        if not has_marker(stage_file(d, st["id"])):
            return "run", f"{st['id']}가 끝나지 않았습니다. 리뷰를 건너뛴 원고는 출판할 수 없습니다."
        if stale_inputs(d, state, st["id"]):
            return "stale", f"{st['id']}가 입력이 바뀐 뒤 그대로입니다."
    if "15_manuscript" not in state.get("stages", {}):
        return "run", "15_manuscript.md가 'book.py merge'로 만들어지지 않았습니다."
    _, toc = load_toc()
    if state.get("toc_sha") and toc and state["toc_sha"] != C.sha(toc):
        return "accept-toc", "책 설정(검증 규칙, 출판 설정)이 바뀌었습니다. 이 책의 설정을 고친 것이면 'book.py accept-toc' 후 다시 검사하세요."
    problem = publish_edit_problem(d, state)
    if problem:
        return "publish-fix", problem
    ms = stage_file(d, "15_manuscript")
    vr = OUTPUT / d.name / "verify-report.md"
    if report_field(vr, "원고 해시") != C.sha(ms) or (toc and report_field(vr, "책 설정 해시") != C.sha(toc)):
        return "verify", "지금 원고와 책 설정으로 만든 verify-report.md가 없습니다. publish.md 1단계를 하세요."
    red, score = fresh_verify(d)
    if red:
        return "verify", f"규칙 검사에 🔴 {red}건이 있습니다."
    if score < 9 and not approval_ok(state, "low-score", ms):
        return "approve-low-score", f"규칙 점수 {score}점(9점 미만)에 대한 사용자 승인이 없습니다."
    if need_facts and not approval_ok(state, "facts", ms):
        return "approve-facts", "출판 전 사실 확인 승인(사람 확인 3)이 없습니다."
    return None


def fresh_verify(d):
    """보고서 텍스트를 믿지 않고 지금 원고를 다시 검사한다. 반환: (🔴 수, 점수)."""
    import tempfile
    import verify
    with tempfile.TemporaryDirectory() as tmp:
        rp = pathlib.Path(tmp) / "r.md"
        with contextlib.redirect_stdout(io.StringIO()):
            toc_path, _ = load_toc()
            verify.main([str(stage_file(d, "15_manuscript")), "--report", str(rp), "--min-score", "0", "--toc", str(toc_path)])
        txt = rp.read_text(encoding="utf-8")
    red = int(re.search(r"^- 🔴 필수: (\d+)", txt, re.M).group(1))
    score = int(re.search(r"^- 규칙 준수 점수: (\d+)", txt, re.M).group(1))
    return red, score


def stage_title(st, state):
    rnd = state.get("ensemble", {}).get("round", 2)
    return st["title"].replace("{round}", str(max(2, rnd)))


# ---------------------------------------------------------------------------
# 선행 조건, 신선도
# ---------------------------------------------------------------------------
def prereq(d, state, sid):
    """sid를 시작해도 되는지. 안 되면 BookError."""
    base_id = re.sub(r"_p\d\d$", "", sid)
    st = STAGE_BY_ID.get(base_id)
    if not st:
        raise BookError(f"알 수 없는 단계: {sid}")
    idx = STAGES.index(st)
    after = st.get("after", [STAGES[idx - 1]["id"]] if idx else [])
    missing = [a for a in after if not has_marker(stage_file(d, a))]
    if missing:
        raise BookError(f"{sid}를 하기 전에 끝내야 할 단계: {', '.join(missing)}. 'book.py status'로 순서를 확인하세요.")
    _, toc = load_toc()
    if state.get("toc_sha") and toc and state["toc_sha"] != C.sha(toc):
        raise BookError("책 설정이 이 책을 시작할 때와 다릅니다. 이 책의 설정을 고친 것이면 'book.py accept-toc', 다른 책의 설정이면 그 책의 작업 폴더에서 하세요.")
    stale = [s["id"] for s in STAGES[:idx] if has_marker(stage_file(d, s["id"])) and stale_inputs(d, state, s["id"])]
    if stale:
        raise BookError(f"앞 단계 {', '.join(stale)}가 입력이 바뀐 뒤 그대로입니다. 'book.py status'의 안내대로 reset부터 하세요.")
    if st["kind"] == "draft" and base_id != sid:
        if not approval_ok(state, "outline", stage_file(d, "02_outline")):
            raise BookError("목차 승인(사람 확인 1)이 없거나, 승인 뒤 02_outline.md가 바뀌었습니다.")
        tp = tone_part()
        if int(sid[-2:]) > tp and not approval_ok(state, "tone", part_path(d, "03_draft-v1", tp)):
            raise BookError(f"톤 승인(사람 확인 2)이 없거나, 승인 뒤 03_draft-v1_p{tp:02d}.md가 바뀌었습니다.")


def record_inputs(state, sid, refs, d):
    state.setdefault("stages", {})[sid] = {"inputs": {r: C.sha(resolve_doc(d, r)) for r in refs}, "at": now()}


def stale_inputs(d, state, sid):
    """완료된 단계가 기록한 입력 중 그 뒤에 바뀐 것."""
    rec = state.get("stages", {}).get(sid)
    if not rec:
        return []
    return [r for r, h in rec["inputs"].items() if C.sha(resolve_doc(d, r)) != h]


def resolve_doc(d, ref):
    p = pathlib.Path(ref)
    if p.suffix == ".md":
        return d / p if (d / p).is_file() else p
    return stage_file(d, ref)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def revise_sources(state, stage):
    """수정 단계의 (기준 원고, 읽을 리뷰 목록, 점수 기록 ID 목록). 합평 3라운드면 백업본을 읽는다."""
    ens = state.get("ensemble", {})
    if stage["id"] == "09_draft-v4" and ens.get("round", 0) >= 3 and ens.get("revise_base"):
        return ens["revise_base"], [ens["revise_review"]], [ens.get("revise_review_id", "10_ensemble-review-2")]
    return stage["base"], stage["reviews"], stage["reviews"]


PHASE_UNIT = {2: "집필 작업", 3: "리뷰와 수정"}


# 진행률 가중치: 사람이 느끼는 작업량에 맞춘다 (초안이 가장 크다). 합계 100.
WEIGHT = {"01_research-notes": 8, "02_outline": 4, "03_draft-v1": 34, "verify": 3, "docx": 4, "metadata": 3}
REVIEW_IDS = [s["id"] for s in STAGES if s["phase"] == 3]
for _sid in REVIEW_IDS:
    WEIGHT[_sid] = 44 / len(REVIEW_IDS)


def progress_text(done, stage_id, parts=None):
    """사람이 읽는 진행률: '4단계 중 3단계(리뷰) · 리뷰와 수정 12번 중 3번째 · 전체의 약 52%'.

    done은 끝난 단계 수, parts는 초안 단계에서 (완료한 부, 전체 부)."""
    order = [s["id"] for s in STAGES] + PUBLISH_STEPS
    pct = sum(WEIGHT[k] for k in order[:done])
    if stage_id == "03_draft-v1" and parts and parts[1]:
        pct += WEIGHT["03_draft-v1"] * parts[0] / parts[1]
    if stage_id == "done":
        return "끝 · 전체의 100%"
    if stage_id in STAGE_BY_ID:
        ph = STAGE_BY_ID[stage_id]["phase"]
        if ph == 3:
            sub = f" · 리뷰와 수정 {len(REVIEW_IDS)}번 중 {REVIEW_IDS.index(stage_id) + 1}번째"
        elif stage_id == "03_draft-v1" and parts:
            sub = f" · 원고 덩어리(프롤로그·부·부록 등) {parts[1]}개 중 {parts[0]}개 완료"
        else:
            sub = ""
    else:
        ph, sub = 4, ""
    return f"4단계 중 {ph}단계({PHASES[ph]}){sub} · 전체의 약 {round(pct)}%"


def report_field(path, key):
    if not path.is_file():
        return None
    m = re.search(rf"^- {re.escape(key)}: (.+)$", path.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else None


def compute_status(d):
    toc_path, toc = load_toc()
    res = {"book": str(d.relative_to(ROOT)) if d else None, "toc": str(toc_path) if toc_path else None, "total": TOTAL_STEPS}

    def finish(done, stage, **kw):
        parts = (len(res.get("parts_done") or []), res.get("divisions") or 0) if stage == "03_draft-v1" else None
        res.update(done=done, stage=stage, progress=progress_text(done, stage, parts), **kw)
        return res

    zips = [z for base in (ROOT, pathlib.Path("/mnt/user-data/uploads"), pathlib.Path("/mnt/data")) if base.is_dir()
            for z in base.glob("book-work-*.zip")]
    if (not toc or C.PLACEHOLDER_RE.search(toc)) and zips and d is None:
        latest = max(zips, key=lambda z: z.stat().st_mtime)
        return finish(0, "01_research-notes", action="import",
                      next=f"작업 묶음 {latest.name}이 있습니다. 이어서 쓰는 중이면 'book.py import {latest}'로 불러오세요.")
    if not toc:
        return finish(0, "01_research-notes", action="interview",
                      next="책 정보가 없습니다. 사용자가 '이어서 써줘'라고 했다면 인터뷰를 시작하지 말고, 가장 최근에 내려받은 "
                           "작업 묶음(book-work-…zip)을 이 대화에 올려 달라고 하세요. 새 책이면 SKILL.md '첫 실행' 절차로 인터뷰한 뒤 "
                           "작업 폴더에 user_input/user-book-toc.md를 만드세요.")
    if C.PLACEHOLDER_RE.search(toc):
        return finish(0, "01_research-notes", action="interview",
                      next="책 정보 파일에 아직 빈칸(중괄호 두 겹)이 있습니다. 사용자가 '이어서 써줘'라고 했다면 인터뷰 대신 가장 최근 "
                           "작업 묶음(book-work-…zip)을 올려 달라고 하세요. 새 책이면 SKILL.md '첫 실행' 인터뷰로 채우세요.")
    if d is None:
        dirs = book_dirs()
        if len(dirs) > 1:
            return finish(0, "01_research-notes", action="choose",
                          next=f"책이 여러 권입니다: {', '.join(p.name for p in dirs)}. 사용자에게 어느 책인지 묻고 'book.py use <폴더명>'.")
        msg = "새 책을 시작합니다. 'book.py init' 후 references/research.md를 실행하세요."
        if DRAFT.is_dir() and any(DRAFT.glob("*.md")):
            msg += " (draft/ 바로 아래 구 버전 산출물은 건드리지 않습니다.)"
        return finish(0, "01_research-notes", action="init", next=msg)

    state = load_state(d)
    if state.get("toc_sha") and state["toc_sha"] != C.sha(toc):
        snap = d / "_toc-snapshot.md"
        diff = sorted(set(toc.splitlines()) - set(snap.read_text(encoding="utf-8").splitlines())) if snap.is_file() else []
        res["warning"] = ("책 설정이 이 책을 시작할 때와 다릅니다" + (f" (바뀐 줄 예: {diff[0][:40]})" if diff else "") +
                          ". 이 책의 설정을 고친 것이면 'book.py accept-toc', 다른 책이면 새 작업 폴더를 쓰세요.")
    divs = divisions()
    res["divisions"] = len(divs)
    weak = [k for k, v in state.get("reviews", {}).items() if str(v.get("isolation", "")).startswith("없음")]
    if weak:
        res["warning"] = (res.get("warning", "") + " " if res.get("warning") else "") + \
            f"격리 없이 쓴 리뷰가 있습니다({', '.join(weak)}). 같은 모델이 쓰고 평가해 점수가 후할 수 있으니 출판 전 사실 확인을 더 꼼꼼히 하세요."
    done = 0
    for st in STAGES:
        sid = st["id"]
        f = stage_file(d, sid)
        complete = has_marker(f, sid)
        title = stage_title(st, state)

        if complete:
            stale = stale_inputs(d, state, sid)
            if stale:
                return finish(done, sid, action="stale", title=title,
                              next=f"{sid}는 {', '.join(stale)}가 바뀌기 전 기준입니다. 사용자에게 이유와 다시 할 리뷰·수정 단계 수"
                                   f"({sum(1 for s in STAGES[STAGES.index(STAGE_BY_ID[sid]):] if has_marker(stage_file(d, s['id'])))}개)를 "
                                   f"알리고 동의를 받은 뒤 'book.py reset {sid}'로 이 단계부터 다시 하세요.")
        if st["kind"] in ("review", "ensemble") and complete:
            rec = state.get("reviews", {}).get(sid)
            if not rec:
                return finish(done, sid, action="score", title=title, next=f"{sid}의 점수 기록이 없습니다. 'book.py score {sid}'.")
            if sid == "10_ensemble-review-2" and not rec.get("passed"):
                rnd = state.get("ensemble", {}).get("round", 2)
                if rnd < 3:
                    return finish(done, sid, action="next-round", title=title,
                                  next=f"합평 {rnd}라운드 미통과 (평균 {rec.get('avg')}, 🔴 {rec.get('red')}). 'book.py next-round'로 3라운드를 준비하세요.")
                res["warning"] = "합평 3라운드 후에도 미통과. 11_draft-final 변경 로그에 미통과 사유를 남기고 진행합니다."

        if complete:
            done += 1
            if sid == "02_outline" and not approval_ok(state, "outline", f):
                return finish(done, sid, action="approve", checkpoint="outline", title=title,
                              next="사람 확인 1: 02_outline.md를 표로 요약해 보여주고 승인받으세요. 승인 후 'book.py approve outline'.")
            continue

        res.update(title=title, doc=st["doc"])
        if st["kind"] in ("draft", "revise"):
            parts = [(i + 1, part_path(d, sid, i + 1)) for i in range(len(divs))]
            open_parts = [n for n, p in parts if p.exists() and not has_marker(p)]
            done_parts = [n for n, p in parts if has_marker(p)]
            res["parts_done"] = done_parts
            if st["kind"] == "draft":
                tp = tone_part()
                if tp in done_parts and not approval_ok(state, "tone", part_path(d, sid, tp)):
                    return finish(done, sid, action="approve", checkpoint="tone",
                                  next=f"사람 확인 2: 03_draft-v1_p{tp:02d}.md(첫 본문 부)의 첫 2쪽 분량을 보여주고 톤을 승인받으세요. 승인 후 'book.py approve tone'.")
                todo = [n for n, p in parts if not has_marker(p)]
                if open_parts:
                    n = open_parts[0]
                    return finish(done, sid, action="write-part", part=n,
                                  next=f"{part_path(d, sid, n).name} 이어쓰기 (부: {divs[n-1]}). 끝까지 쓴 뒤 'book.py complete {sid}_p{n:02d}'.")
                if todo:
                    n = todo[0]
                    return finish(done, sid, action="write-part", part=n, remaining=todo,
                                  next=f"{part_path(d, sid, n).name} 작성 (부: {divs[n-1]}). 끝까지 쓴 뒤 'book.py complete {sid}_p{n:02d}'.")
                return finish(done, sid, action="merge", next=f"모든 조각 완료. 'book.py merge {sid}'로 합치세요.")
            base, reviews, _ = revise_sources(state, st)
            res["inputs"] = {"base": base, "reviews": reviews}
            if open_parts:
                n = open_parts[0]
                return finish(done, sid, action="write-part", part=n,
                              next=f"{part_path(d, sid, n).name} 이어쓰기. 끝나면 'book.py complete {sid}_p{n:02d}'.")
            return finish(done, sid, action="revise",
                          next=(f"{', '.join(reviews)}의 지적을 반영합니다. 지적이 있는 부만 {sid}_pNN.md로 고쳐 쓰고, "
                                f"_changelog.md의 '## {sid}' 표에 🔴 지적 ID를 모두 ✅로 남긴 뒤 'book.py merge {sid}'."))
        if f.exists():
            nxt = f"{f.name}에 완료 마커가 없습니다. 같은 파일에 이어서 쓰세요 (덮어쓰기 금지)."
            action = "resume"
        else:
            nxt = f"{title}: {st['doc']} 문서를 따르세요."
            action = "run"
        if st["kind"] in ("review", "ensemble"):
            nxt += f" 다 쓰면 'book.py score {sid}' (리뷰어는 점수를 쓰지 않음)."
        else:
            nxt += f" 다 쓰면 'book.py complete {sid}'."
        return finish(done, sid, action=action, inputs={"inputs": st.get("inputs", [])}, next=nxt)

    # 출판
    out = OUTPUT / d.name
    ms = stage_file(d, "15_manuscript")
    ms_sha = C.sha(ms)
    gate = publish_gate(d, state, need_facts=False)
    if gate:
        action, why = gate
        if action == "approve-low-score":
            return finish(done, "publish-verify", action="approve", checkpoint="low-score",
                          next=f"{why} 🟡 목록을 보여주고 고칠지 물으세요. 이대로 가기로 하면 'book.py approve low-score --note \"사유\"'.")
        nxt = {"verify": "references/publish.md 1단계: verify.py로 15_manuscript.md를 검사하세요. ",
               "publish-fix": "", "accept-toc": ""}.get(action, "")
        return finish(done, "publish-verify", action=action, next=nxt + why)
    missing = missing_figures(d, ms)
    if missing:
        return finish(done, "publish-figures", action="figures",
                      next=f"references/publish.md 1.1: 그림을 먼저 만드세요 (없는 그림 {len(missing)}개: "
                           f"{', '.join(f'fig{n:02d}' for n in missing[:8])}). 그림 속 수치도 사실 확인 대상입니다.")
    done += 1
    if not approval_ok(state, "facts", ms):
        return finish(done, "publish-facts", action="approve", checkpoint="facts",
                      next="사람 확인 3: 'book.py facts'로 사실 확인 표를 만들어 보여주고 승인받으세요. 승인 후 'book.py approve facts'.")
    br = out / "build-report.md"
    if not ((out / "final.docx").is_file() and report_field(br, "원고 해시") == ms_sha and report_field(br, "판정") == "통과"
            and report_field(br, "결과 해시") == C.sha(out / "final.docx")):
        return finish(done, "publish-docx", action="build",
                      next="references/publish.md 2단계: build_docx.py로 final.docx를 만드세요.")
    done += 1
    md_meta = out / "metadata.md"
    if not md_meta.is_file() or md_meta.stat().st_mtime < (out / "final.docx").stat().st_mtime:
        return finish(done, "publish-metadata", action="metadata", next="references/publish.md 3단계: metadata.md를 쓰세요.")
    leaks = metadata_leaks(d, md_meta)
    if leaks:
        return finish(done, "publish-metadata", action="metadata",
                      next=f"metadata.md에 새면 안 되는 표현이 있습니다: {'; '.join(leaks[:6])}. 고친 뒤 다시 status를 확인하세요.")
    return finish(TOTAL_STEPS, "done", action="done", next=f"완료. {out.relative_to(ROOT)}/final.docx")


def metadata_leaks(d, md_meta):
    """metadata.md의 🔴 유출 (금지 용어, 쓰면 안 되는 것, 엄격 모드의 실명)."""
    import verify
    _, toc = load_toc()
    rules = dict(verify.RULE_DEFAULTS)
    rules.update({k: v for k, v in C.parse_kv(C.get_section(toc or "", "검증 규칙"), verify.RULE_DEFAULTS.keys())[0].items() if v})
    sdir = C.input_dir(ROOT) / "sources"
    files = [x for x in sdir.rglob("*") if x.is_file() and not x.name.startswith(".")] if sdir.is_dir() else []
    files += [x for x in [d / "01_research-notes.md"] if x.is_file()]
    P = verify.leak_policy(toc or "", rules, files)
    return sorted({f"{cat}: {re.sub(r' [(].*$', '', det)}" for sev, cat, det, _ in verify.leak_scan(md_meta.read_text(encoding="utf-8"), P) if sev == "🔴"})


def cmd_status(a):
    res = compute_status(active_dir())
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"책 폴더: {res.get('book') or '(없음)'}")
    print(f"진행: {res['progress']}")
    if res.get("title"):
        print(f"단계: {res['title']}")
    if res.get("parts_done") is not None and res.get("divisions"):
        print(f"조각: {len(res['parts_done'])}/{res['divisions']} 부 완료")
    if res.get("inputs"):
        inp = res["inputs"]
        if "base" in inp:
            print(f"입력: 기준 원고 {inp['base']}, 리뷰 {', '.join(inp['reviews'])}")
        elif inp.get("inputs"):
            print(f"입력: {', '.join(inp['inputs'])}")
    if res.get("warning"):
        print(f"주의: {res['warning']}")
    print(f"다음: {res['next']}")
    return 0


# ---------------------------------------------------------------------------
# init / use / config / approve / accept-toc
# ---------------------------------------------------------------------------
def cmd_init(a):
    _, toc = load_toc()
    if not toc or C.PLACEHOLDER_RE.search(toc):
        raise BookError("책 정보가 비어 있습니다. 먼저 SKILL.md '첫 실행' 인터뷰로 user_input/user-book-toc.md를 채우세요.")
    today = a.date or datetime.date.today().strftime("%Y%m%d")
    name, n = today, 1
    while (DRAFT / name).exists():
        name = f"{today}_{n:02d}"
        n += 1
    d = DRAFT / name
    d.mkdir(parents=True)
    (OUTPUT / name / "images").mkdir(parents=True, exist_ok=True)
    (DRAFT / ".active").write_text(name + "\n", encoding="utf-8")
    (d / "_toc-snapshot.md").write_text(toc, encoding="utf-8")
    save_state(d, {"created_at": now(), "toc_sha": C.sha(toc), "config": {}, "approvals": {}, "reviews": {}, "stages": {}})
    print(f"활성 프로젝트 폴더: draft/{name}/")
    rng = C.target_chars(toc)
    parts = len(C.toc_structure(toc))
    if rng:
        hi = rng[1]
        print(f"사용자 안내: 목표 분량은 {rng[0]:,}~{hi:,}자이고, 원고 덩어리(프롤로그, 각 부, 에필로그, 부록 등)는 {parts}개입니다. "
              f"'이어서 해줘'를 약 {parts + 11}번 말씀하시면 끝납니다 (덩어리마다 1번 = {parts}번, 리뷰와 수정 약 7번, 출판 약 4번). "
              f"리뷰에서 원고 전체를 15번 읽기 때문에 사용량이 가장 많이 드는 구간은 리뷰입니다 (읽는 양은 대략 {hi * 15 // 10000:,}만 자).")
    return 0


def cmd_use(a):
    if not BOOK_DIR_RE.match(a.name) or not (DRAFT / a.name).is_dir():
        raise BookError(f"책 폴더가 아닙니다: {a.name}. 있는 책: {', '.join(p.name for p in book_dirs()) or '(없음)'}")
    (DRAFT / ".active").write_text(a.name + "\n", encoding="utf-8")
    st = load_state(DRAFT / a.name)
    log_event(st, "use")
    save_state(DRAFT / a.name, st)
    print(f"활성 프로젝트 폴더: draft/{a.name}/")
    return 0


def cmd_config(a):
    d = require_active()
    state = load_state(d)
    cfg = state.setdefault("config", {})
    for kv in a.pairs:
        if "=" not in kv:
            raise BookError(f"키=값 형식이 아닙니다: {kv}")
        k, v = kv.split("=", 1)
        if k == "chunk_unit" and v in ("part", "two_parts"):
            cfg[k] = v
        elif k == "auto_continue" and v in ("true", "false"):
            cfg[k] = v == "true"
        else:
            raise BookError(f"허용되지 않는 설정: {kv} (chunk_unit=part|two_parts, auto_continue=true|false)")
    cfg["decided_at"] = now()
    save_state(d, state)
    print(f"작업 설정 저장: chunk_unit={cfg.get('chunk_unit')}, auto_continue={cfg.get('auto_continue')}")
    return 0


APPROVAL_TARGET = {"outline": "02_outline", "tone": "03_draft-v1_pNN", "facts": "15_manuscript", "low-score": "15_manuscript"}


def cmd_approve(a):
    d = require_active()
    name = APPROVAL_TARGET[a.what].replace("NN", f"{tone_part():02d}")
    target = d / f"{name}.md"
    if not has_marker(target):
        raise BookError(f"승인할 대상 {target.name}이 아직 완료되지 않았습니다.")
    state = load_state(d)
    if a.what in ("facts", "low-score"):
        gate = publish_gate(d, state, need_facts=False)
        if gate and not (a.what == "low-score" and gate[0] == "approve-low-score"):
            raise BookError(f"아직 승인할 단계가 아닙니다: {gate[1]}")
        if a.what == "low-score" and not gate:
            raise BookError("규칙 점수가 9점 이상이라 낮은 점수 승인이 필요 없습니다.")
    if a.what == "facts":
        ft = state.get("facts_table", {})
        _, toc_cur = load_toc()
        if ft.get("ms") != C.sha(target) or ft.get("evidence") != evidence_sha(d) or ft.get("toc") != C.sha(toc_cur or "") \
                or ft.get("file") != C.sha(OUTPUT / d.name / "fact-check.md"):
            raise BookError("사실 확인 표가 없거나 지금 원고·그림·출처·설정으로 만든 것이 아닙니다. 'book.py facts'로 표를 다시 만들어 "
                            "사용자에게 보여 준 뒤 승인을 받으세요.")
        miss = missing_figures(d, target)
        if miss:
            raise BookError(f"그림이 아직 없습니다 ({', '.join(f'fig{n:02d}' for n in miss[:8])}). 그림 속 수치도 사실 확인 대상이라 "
                            f"그림을 만든 뒤 'book.py facts'로 표를 다시 만들고 승인을 받으세요.")
    if a.what == "low-score" and not a.note:
        raise BookError("9점 미만으로 진행하는 사유를 --note로 남기세요.")
    _, toc_now = load_toc()
    state.setdefault("approvals", {})[a.what] = {"at": now(), "sha": C.sha(target), "note": a.note or "",
                                                 "toc_sha": C.sha(toc_now) if toc_now else None,
                                                 "evidence_sha": evidence_sha(d)}
    extra = {}
    if a.what == "facts" and (d / "_15_manuscript.merged.md").is_file():
        import difflib
        diff = list(difflib.unified_diff((d / "_15_manuscript.merged.md").read_text(encoding="utf-8").splitlines(),
                                         target.read_text(encoding="utf-8").splitlines(), lineterm="", n=0))
        extra = {"edited_after_merge": {"added": sum(1 for x in diff if x.startswith("+") and not x.startswith("+++")),
                                        "removed": sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))}}
    if a.what == "facts":
        accept_publish_edits(d, state)
    log_event(state, "approve", what=a.what, target=target.name, note=a.note or "", **extra)
    save_state(d, state)
    print(f"승인 기록: {a.what} ({target.name})")
    return 0


def cmd_accept_toc(a):
    d = require_active()
    _, toc = load_toc()
    state = load_state(d)
    old = (d / "_toc-snapshot.md").read_text(encoding="utf-8") if (d / "_toc-snapshot.md").is_file() else ""
    changed = sorted(set(old.splitlines()) ^ set(toc.splitlines()))
    log_event(state, "accept-toc", old_sha=state.get("toc_sha"), new_sha=C.sha(toc), changed_lines=changed[:40])
    state["toc_sha"] = C.sha(toc)
    (d / "_toc-snapshot.md").write_text(toc, encoding="utf-8")
    save_state(d, state)
    print("현재 책 설정을 이 책의 기준으로 기록했습니다.")
    return 0


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------
def cmd_complete(a):
    d = require_active()
    sid = a.sid[:-3] if a.sid.endswith(".md") else a.sid
    base_id = re.sub(r"_p\d\d$", "", sid)
    st = STAGE_BY_ID.get(base_id)
    if not st:
        raise BookError(f"알 수 없는 단계: {sid}")
    if st["kind"] in ("review", "ensemble"):
        raise BookError(f"리뷰 단계는 'book.py score {base_id}'로 마무리합니다.")
    if st["kind"] in ("draft", "revise") and sid == base_id:
        raise BookError(f"원고 버전은 'book.py merge {sid}'로 만듭니다. 조각은 '{sid}_pNN'으로 완료하세요.")
    state = load_state(d)
    prereq(d, state, sid)
    f = d / f"{sid}.md"
    if not f.is_file() or not f.read_text(encoding="utf-8").strip():
        raise BookError(f"{f.name}이 없거나 비어 있습니다.")
    if has_marker(f, sid):
        print(f"이미 완료: {f.name}")
        return 0
    text = C.strip_markers(f.read_text(encoding="utf-8")).rstrip()
    if sid != base_id:
        check_part(text, int(sid[-2:]), f.name)
    f.write_text(text + f"\n\n<!-- STAGE_COMPLETE: {sid} -->\n", encoding="utf-8")
    if sid == base_id:
        record_inputs(state, sid, st.get("inputs", []), d)
        save_state(d, state)
    print(f"완료 마커 추가: {f.name}")
    return 0


def check_part(text, n, name):
    divs = divisions()
    if n > len(divs):
        raise BookError(f"목차의 부는 {len(divs)}개인데 p{n:02d}입니다.")
    h1 = [t for _, lv, t in C.iter_headings(text) if lv == 1]
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not first.startswith("# ") or label(first[2:]) != label(divs[n - 1]):
        raise BookError(f"{name}의 첫 줄은 '# {divs[n-1]}' 헤딩이어야 합니다 (지금: '{first[:40]}').")
    if len(h1) != 1:
        raise BookError(f"{name}에 '# ' 부 헤딩이 {len(h1)}개입니다. 조각 하나에는 부 헤딩이 하나만 있어야 합니다.")


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
def locate(loc):
    """지적 위치 문자열을 부 번호 목록(1부터)으로. 책 전체 지적이면 '*', 못 찾으면 None.

    '[프롤로그~제2부]'처럼 여러 부를 가리키면 모두 돌려준다. 수정 단계는 그 부를 모두 고쳐야 한다.
    """
    key = C.despace(loc)
    _, toc = load_toc()
    struct = C.toc_structure(toc) if toc else []
    found = set()
    for i, (div, chapters) in enumerate(struct):
        if label(div) and label(div) in key:
            found.add(i + 1)
        for ch in chapters:
            if C.despace(C.split_label(ch)[0]) in key:
                found.add(i + 1)
    if found and re.search(r"[~∼〜\-–—]|부터|까지|에서", loc):
        # '프롤로그~제2부'처럼 범위면 사이의 부도 모두 포함한다
        found = set(range(min(found), max(found) + 1))
    if found:
        return sorted(found)
    # 부나 장 이름이 없고, 위치 칸 전체가 '전체' 같은 말일 때만 책 전체 지적으로 본다 ('제1부 전반'은 제1부다)
    if key in WHOLE_BOOK:
        return "*"
    return None


def parse_findings(text, sid, ensemble):
    """지적 줄을 찾아 ID를 다시 매긴다. 반환: (ID를 넣은 본문, [(평가자 번호, ID, 심각도, 부 번호)]).

    지적 줄 밖(표, 코드 블록, 설명 문장)에 심각도 기호가 있으면 채점을 거부한다.
    형식이 어긋난 지적이 조용히 빠진 채 점수가 매겨지는 것을 막기 위해서다.
    """
    tag = sid[:2]
    out, findings, stray, unplaced = [], [], [], []
    in_code = False
    reviewer = 0
    counters = {}
    section = "pre"   # pre: 첫 '## ' 앞 (첫 지적 전까지 머리말 허용), free: 총평 등, strict: 지적 섹션
    last = None       # 바로 앞의 의미 있는 줄: finding | sub | None
    for no, line in enumerate(text.split("\n"), 1):
        fence = line.lstrip().startswith(("```", "~~~"))
        if fence:
            in_code = not in_code
        if not in_code and not fence and line.startswith("## "):
            section = "free" if C.despace(line[3:]) in FREE_SECTIONS else "strict"
            last = None
        m_rev = None if in_code else REVIEWER_RE.match(line)
        if m_rev:
            reviewer = int(m_rev.group(1))
        m = None if (in_code or fence or line.lstrip().startswith("|")) else FINDING_LINE_RE.match(line)
        if not m:
            strict = section == "strict" or (section == "pre" and bool(findings))
            body = line.strip()
            if any(c in line for c in SEV) or SEV_SYMBOL_RE.search(line) or (not in_code and SEV_TEXT_RE.search(line)):
                stray.append(no)
            elif strict and (in_code or fence):
                stray.append(no)  # 지적 섹션에 코드 블록을 두지 않는다 (지적을 숨길 수 있다)
            elif strict and body and not line.startswith("## "):
                if (section == "pre" and (line.startswith("# ") or body.startswith("격리:"))) \
                        or re.fullmatch(r"[-*_]{3,}", body) or re.fullmatch(r"지적 없음\.?", body):
                    pass
                elif SUBITEM_RE.match(line) and last in ("finding", "sub"):
                    last = "sub"
                elif line[:1] in (" ", "\t") and last in ("finding", "sub") and not re.match(r"#|\||>|```|~~~", body):
                    last = "sub"  # 바로 위 지적·하위 항목의 이어지는 줄
                else:
                    stray.append(no)  # 지적 섹션의 헤딩·홀로 쓴 하위 항목·자유 문장 (형식 밖 지적일 수 있다)
            out.append(line)
            continue
        last = "finding"
        sev = m.group(1) or SEV_WORD[m.group(2)]
        line = re.sub(r"\s*\[" + FINDING_ID_RE.pattern + r"\]", "", line, count=1)  # 다시 채점할 때 옛 ID 제거
        m = FINDING_LINE_RE.match(line)
        if ensemble and reviewer == 0:
            raise BookError(f"{no}행: 합평 지적은 '## 평가자 N: 이름' 섹션 안에 써야 합니다 (평가자 1 앞에 지적이 있음).")
        key = reviewer if ensemble else 0
        counters[key] = counters.get(key, 0) + 1
        fid = f"R{tag}-{reviewer}-{counters[key]:02d}" if ensemble else f"R{tag}-{counters[key]:02d}"
        rest = line[m.end():]
        loc_m = re.match(r"[\s:：\-–—]*\[([^\]]+)\]", rest)
        loc = loc_m.group(1) if loc_m else re.split(r"\s[-–—]\s|[.:]", rest.strip(" *:"), maxsplit=1)[0]
        part = locate(loc) if loc else None
        if sev == "🔴" and part is None:
            unplaced.append(f"{no}행 {fid}")
        if any(c in rest for c in SEV):
            stray.append(no)
        out.append(line[:m.end()] + f" [{fid}]" + rest)
        findings.append((key, fid, sev, part, re.sub(r"\s+", " ", rest).strip()[:160]))
    if stray:
        raise BookError(f"지적 형식에 맞지 않는 줄이 있습니다 ({', '.join(map(str, stray[:8]))}행). 지적 섹션에는 "
                        f"'[🔴] [위치] - 제목' 지적 줄과 바로 그 아래 하위 항목('- 원문:', '- 문제점:', '- 수정 제안:', '- 근거:' 등)만 씁니다. "
                        f"설명이나 총평은 '## 총평' 섹션에 쓰고, 심각도 표시(🔴/🟡/🟢, '필수:', '(심각도: 높음)', '[Critical]' 등)는 지적 줄 맨 앞에만 둡니다 "
                        f"(형식이 어긋난 지적이 집계에서 빠지는 것을 막는다). 서브에이전트가 쓴 리뷰라면 손으로 고치지 말고 "
                        f"이 메시지를 붙여 같은 리뷰어를 다시 띄운다.")
    if unplaced:
        raise BookError(f"🔴 지적의 위치에 부나 장 이름이 없습니다: {', '.join(unplaced[:8])}. '[제2부 제5장 …]'처럼 쓰거나, 책 전체에 걸친 지적이면 '[전체]'로 쓰세요. "
                        f"수정 단계가 그 부를 실제로 고쳤는지 확인하는 데 쓴다.")
    return "\n".join(out), findings


def score_block_sha(block):
    """book.py가 쓴 점수 블록의 해시 (완료 마커와 빈 줄, 앞뒤 공백은 빼고 본다)."""
    rows = [ln.strip() for ln in C.strip_markers(block).splitlines() if ln.strip()]
    return C.sha("\n".join(rows))


def cmd_score(a):
    d = require_active()
    sid = a.sid[:-3] if a.sid.endswith(".md") else a.sid
    st = STAGE_BY_ID.get(sid)
    if not st or st["kind"] not in ("review", "ensemble"):
        raise BookError(f"{sid}는 리뷰 단계가 아닙니다.")
    state = load_state(d)
    prereq(d, state, sid)
    f = stage_file(d, sid)
    if not f.is_file():
        raise BookError(f"{f.name}이 없습니다.")
    prev = state.get("reviews", {}).get(sid)
    if prev and stale_inputs(d, state, sid):
        raise BookError(f"리뷰한 뒤 원고가 바뀌었습니다. 같은 리뷰를 다시 채점할 수 없습니다. 'book.py reset {sid}' 후 새로 리뷰하세요.")
    text = C.strip_markers(f.read_text(encoding="utf-8"))
    if SCORE_HEAD in text:
        cut = text.rindex(SCORE_HEAD)
        if not prev or prev.get("block_sha") != score_block_sha(text[cut:]):
            raise BookError(f"'{SCORE_HEAD}' 섹션이 book.py가 쓴 그대로가 아닙니다 (뒤에 줄이 붙었거나 고쳐졌다). "
                            f"지적은 점수 섹션 위의 지적 섹션에 쓰고, 점수 섹션은 지운 뒤 다시 채점하세요 (점수 섹션 안의 지적은 집계에서 빠진다).")
        text = text[:cut]
    text = text.rstrip()
    ensemble = st["kind"] == "ensemble"
    if ensemble:
        nums = [int(m.group(1)) for m in REVIEWER_RE.finditer(text)]
        if sorted(nums) != [1, 2, 3, 4, 5]:
            raise BookError(f"합평은 '## 평가자 1: 이름' ~ '## 평가자 5: 이름' 섹션이 하나씩 있어야 합니다 (지금 {nums}).")
    text, findings = parse_findings(text, sid, ensemble)
    # '지적 없음'은 단독 줄일 때만 인정한다. 합평은 평가자마다 따로 확인한다
    sections = [(0, text)]
    if ensemble:
        heads = list(REVIEWER_RE.finditer(text))
        sections = [(int(h.group(1)), text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]) for i, h in enumerate(heads)]
    for rv, body in sections:
        has = any(k == rv for k, *_ in findings) if ensemble else bool(findings)
        if not has and not re.search(r"^\s*지적 없음\.?\s*$", body, re.M):
            who = f"평가자 {rv}" if ensemble else "이 리뷰"
            raise BookError(f"{who}의 지적이 하나도 집계되지 않았습니다. 정말 지적이 없으면 '지적 없음'을 한 줄로 쓰고, 아니면 지적 줄을 "
                            "'[🔴] [위치] - 제목' 형식으로 고치세요 (형식이 어긋난 지적이 0건으로 채점되는 것을 막는다).")
    iso = re.search(r"^격리:\s*(.+)$", text, re.M)
    if ensemble and not iso:
        raise BookError("합평 파일 첫머리에 '격리: 서브에이전트 5개' 또는 '격리: 없음 (…)' 줄이 있어야 합니다.")

    from verify import anchor_score
    rows = []
    if ensemble:
        names = {int(m.group(1)): m.group(2) for m in REVIEWER_RE.finditer(text)}
        for k in range(1, 6):
            c = {s: sum(1 for r, _, sv, *_ in findings if r == k and sv == s) for s in SEV}
            rows.append((names[k], c, anchor_score(c["🔴"], c["🟡"])))
    else:
        c = {s: sum(1 for _, _, sv, *_ in findings if sv == s) for s in SEV}
        rows.append((st["title"], c, anchor_score(c["🔴"], c["🟡"])))
    red = sum(r[1]["🔴"] for r in rows)
    yellow = sum(r[1]["🟡"] for r in rows)
    avg = round(sum(r[2] for r in rows) / len(rows), 1)
    passed = red == 0 and avg >= 9

    new_red = {fid: (part, txt) for _, fid, sv, part, txt in findings if sv == "🔴"}
    changed = []
    if prev:
        old_red = {f["id"]: (f.get("part"), f.get("text")) for f in prev.get("findings", []) if f["sev"] == "🔴"}
        changed = sorted(fid for fid in old_red if fid not in new_red or new_red[fid] != old_red[fid])
    if prev and (red < prev.get("red", 0) or changed) and not a.reason:
        raise BookError(f"다시 채점했더니 🔴 지적이 줄었거나 위치나 내용이 바뀌었습니다 ({prev['red']}건 → {red}건, 바뀐 지적: {', '.join(changed) or '없음'}). "
                        f"이유를 --reason으로 남기세요 (기록에 남는다).")
    if prev:
        backup(d, f)  # 다시 채점하기 전의 리뷰 원문을 남긴다

    out = [SCORE_HEAD, "", "| 평가자 | 🔴 | 🟡 | 🟢 | 점수 |", "|---|---|---|---|---|"]
    out += [f"| {n} | {c['🔴']} | {c['🟡']} | {c['🟢']} | {s} |" for n, c, s in rows]
    out += ["", f"- 평균 점수: {avg} / 10", f"- 🔴 합계: {red}건"]
    out.append("- 🔴 지적 ID: " + (", ".join(fid for _, fid, sv, *_ in findings if sv == "🔴") or "없음"))
    if ensemble:
        out.append(f"- 판정: {'통과' if passed else '재수정'} (🔴 0건이고 평균 9점 이상이면 통과)")
    if sid == "10_ensemble-review-2":
        out.append(f"- 라운드: {max(2, state.get('ensemble', {}).get('round', 2))}")
    out += ["", f"<!-- STAGE_COMPLETE: {sid} -->"]
    f.write_text(text + "\n\n" + "\n".join(out) + "\n", encoding="utf-8")
    block_sha = score_block_sha("\n".join(out))

    rec = {"red": red, "yellow": yellow, "avg": avg, "passed": passed, "at": now(),
           "isolation": iso.group(1).strip() if iso else "", "body_sha": C.sha(text), "block_sha": block_sha,
           "findings": [{"id": fid, "sev": sv, "part": part, "text": txt} for _, fid, sv, part, txt in findings]}
    log_event(state, "score", sid=sid, red=red, yellow=yellow, avg=avg, reason=a.reason or "", body_sha=rec["body_sha"],
              changed_red=changed, isolation=rec["isolation"])
    state.setdefault("reviews", {})[sid] = rec
    record_inputs(state, sid, st.get("inputs", []), d)
    if sid == "08_ensemble-review-1":
        state.setdefault("ensemble", {}).setdefault("round", 1)
    if sid == "10_ensemble-review-2":
        ens = state.setdefault("ensemble", {})
        ens["round"] = max(2, ens.get("round", 2))
        ens.setdefault("history", []).append({"round": ens["round"], **{k: v for k, v in rec.items() if k != "findings"}})
    save_state(d, state)
    verdict = ("통과" if passed else "재수정") if ensemble else "기록"
    print(f"점수 기록: {sid} 평균 {avg}/10, 🔴 {red}, 🟡 {yellow}, {verdict}")
    return 0


# ---------------------------------------------------------------------------
# merge / split
# ---------------------------------------------------------------------------
def changelog_check(d, sid, review_ids, state):
    """리뷰의 🔴 지적 ID가 _changelog.md '## sid' 표에 모두 ✅로 있어야 한다. 반환: 🔴 ID 집합."""
    reds, known, parts_of_red = set(), set(), {}
    for r in review_ids:
        for fnd in state.get("reviews", {}).get(r, {}).get("findings", []):
            known.add(fnd["id"])
            if fnd["sev"] == "🔴":
                reds.add(fnd["id"])
                parts_of_red[fnd["id"]] = fnd.get("part")
    log = d / "_changelog.md"
    sec = last_section(log.read_text(encoding="utf-8"), sid) if log.is_file() else None
    if sec is None:
        raise BookError(f"_changelog.md에 '## {sid}' 섹션이 없습니다. 지적이 없었어도 '변경 없음' 한 줄을 남기세요.")
    rows = [r.strip() for r in sec.splitlines() if r.strip().startswith("|")]
    header = next((r for r in rows if "반영" in r and "심각도" in r), None)
    done_ids = set()
    if header:
        cols = [c.strip() for c in header.strip("|").split("|")]
        ci = next(i for i, c in enumerate(cols) if c.startswith("반영"))
        why = next((i for i, c in enumerate(cols) if c.startswith("사유")), None)
        for r in rows[rows.index(header) + 1:]:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            # 사유 칸에 적은 다른 지적 ID('R04-01 수정으로 해소')는 이 행의 대상이 아니다
            ids = set(FINDING_ID_RE.findall(" | ".join(c for i, c in enumerate(cells) if i != why)))
            unknown = ids - known
            if unknown:
                raise BookError(f"변경 로그에 이 단계의 리뷰에 없는 지적 ID가 있습니다: {', '.join(sorted(unknown))}")
            status = cells[ci] if ci < len(cells) else ""
            checked = re.fullmatch(r"✅\s*(반영|완료)?", status) is not None  # '✅ 아님', '✅ 미반영'은 반영이 아니다
            if ids & reds and not checked:
                raise BookError(f"🔴 지적 {', '.join(sorted(ids & reds))}의 반영 칸이 ✅가 아닙니다 ('{status}'). 🔴은 모두 반영해야 합니다.")
            if checked:
                done_ids |= ids
    missing = reds - done_ids
    if missing:
        raise BookError(f"변경 로그에 ✅로 기록되지 않은 🔴 지적: {', '.join(sorted(missing))}. "
                        f"표('| 항목 | 심각도 | 출처 | 반영 | 사유 |')의 한 행에 지적 ID를 적고 반영 칸에 ✅를 쓰세요.")
    return parts_of_red


def last_section(md, title):
    """같은 제목의 섹션이 여러 번 있으면 마지막 것 (덧붙여 고친 표를 읽는다)."""
    lines = md.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(rf"^##\s+{re.escape(title)}\s*$", ln)]
    if not starts:
        return None
    i = starts[-1]
    end = next((j for j in range(i + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[j])), len(lines))
    return "\n".join(lines[i + 1:end])


def run_verify(d, path):
    import verify
    report = d / f"_verify-{path.stem}.md"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = verify.main([str(path), "--report", str(report), "--min-score", "0"])
    return code, report, buf.getvalue().strip()


def cmd_merge(a):
    d = require_active()
    sid = a.sid
    st = STAGE_BY_ID.get(sid)
    if not st or st["kind"] not in ("draft", "revise"):
        raise BookError(f"{sid}는 원고 단계가 아닙니다.")
    state = load_state(d)
    prereq(d, state, sid)
    divs = divisions()
    if not divs:
        raise BookError("목차에서 부(### 헤딩)를 읽지 못했습니다.")
    labels = [label(t) for t in divs]
    if len(set(labels)) != len(labels):
        raise BookError("목차에 같은 라벨의 부가 두 번 있습니다.")
    parts = {}
    for i in range(len(divs)):
        p = part_path(d, sid, i + 1)
        if p.exists():
            if not has_marker(p):
                raise BookError(f"{p.name}에 완료 마커가 없습니다. 끝까지 쓴 뒤 'book.py complete {p.stem}'.")
            body = C.strip_markers(p.read_text(encoding="utf-8")).strip()
            check_part(body, i + 1, p.name)
            parts[labels[i]] = body
    stray = [p.name for p in d.glob(f"{sid}_p*.md") if int(re.search(r"_p(\d+)", p.stem).group(1)) > len(divs)]
    if stray:
        raise BookError(f"목차보다 많은 조각이 있습니다: {', '.join(stray)}")

    inputs = list(st.get("inputs", []))
    if st["kind"] == "draft":
        missing = [f"p{i+1:02d}" for i in range(len(divs)) if labels[i] not in parts]
        if missing:
            raise BookError(f"아직 안 쓴 조각: {', '.join(missing)}")
        front, base = "", {}
        base_text = None
    else:
        base_ref, review_refs, review_ids = revise_sources(state, st)
        red_parts = changelog_check(d, sid, review_ids, state)
        base_path = resolve_doc(d, base_ref)
        if not base_path.is_file():
            raise BookError(f"기준 원고 {base_path.name}이 없습니다.")
        base_text = C.strip_markers(base_path.read_text(encoding="utf-8")).strip()
        segs = C.manuscript_divisions(base_text)
        front = "".join(t for title, t in segs if title is None).strip()
        base = {}
        for title, t in segs:
            if title is None:
                continue
            lb = label(title)
            if lb in base:
                raise BookError(f"기준 원고에 같은 부가 두 번 있습니다: {title}")
            base[lb] = t.strip()
        extra = [lb for lb in base if lb not in labels]
        if extra:
            raise BookError(f"기준 원고에 목차에 없는 부가 있습니다: {', '.join(extra)}. 목차를 바꿨다면 해당 부를 정리한 조각을 쓰세요.")
        lacking = [lb for lb in labels if lb not in base and lb not in parts]
        if lacking:
            raise BookError(f"목차의 부 {', '.join(lacking)}가 기준 원고에도 조각에도 없습니다.")
        if red_parts and not parts:
            raise BookError(f"🔴 지적이 {len(red_parts)}건인데 고쳐 쓴 조각({sid}_pNN.md)이 하나도 없습니다.")
        # 🔴이 가리킨 부는 그 부의 조각이 실제로 달라져야 한다 (공백만 바뀐 것은 고친 것으로 보지 않는다)
        untouched = {}
        for fid, part in red_parts.items():
            for pn in ([part] if isinstance(part, int) else part if isinstance(part, list) else []):
                if pn > len(labels):
                    continue
                lb = labels[pn - 1]
                if lb not in parts or C.despace(parts[lb]) == C.despace(base.get(lb, "")):
                    untouched.setdefault(f"p{pn:02d}", []).append(fid)
        if untouched:
            detail = "; ".join(f"{p} ({', '.join(ids)})" for p, ids in sorted(untouched.items()))
            raise BookError(f"🔴 지적이 가리킨 부를 고치지 않았습니다: {detail}. 해당 부의 조각을 쓰고 지적된 곳을 실제로 고치세요.")
        inputs = [base_ref] + review_refs

    body = "\n\n".join(([front] if front else []) + [parts.get(lb) or base[lb] for lb in labels]).rstrip()
    if base_text is not None and st["kind"] == "revise" and parts and C.despace(body) == C.despace(base_text):
        raise BookError("고쳐 쓴 조각이 기준 원고와 똑같습니다. 지적된 곳을 실제로 고치세요.")
    out = stage_file(d, sid)
    if out.exists():
        backup(d, out)
    out.write_text(body + f"\n\n<!-- STAGE_COMPLETE: {sid} -->\n", encoding="utf-8")

    code, report, line = run_verify(d, out)
    if code == 1:
        out.write_text(body + "\n", encoding="utf-8")  # 마커를 떼서 미완료로 둔다
        raise BookError(f"병합본에 규칙 위반 🔴이 있습니다. {report.name}의 줄 번호를 보고 해당 조각을 고친 뒤 다시 merge 하세요.\n{line}")
    record_inputs(state, sid, inputs, d)
    state["stages"][sid]["output_sha"] = C.sha(out)
    if sid == "15_manuscript":
        shutil.copy2(out, d / "_15_manuscript.merged.md")  # 출판 전 직접 수정과 비교할 기준본
    log_event(state, "merge", sid=sid, parts=sorted(parts), output_sha=C.sha(out))
    save_state(d, state)
    changed = ", ".join(f"p{labels.index(lb)+1:02d}" for lb in parts) or "없음"
    print(f"병합 완료: {out.name} (새로 쓴 조각: {changed})\n{line}")
    return 0


def cmd_split(a):
    d = require_active()
    src = resolve_doc(d, a.sid)
    if not src.is_file():
        raise BookError(f"{src.name}이 없습니다.")
    segs = [(t, s) for t, s in C.manuscript_divisions(C.strip_markers(src.read_text(encoding="utf-8"))) if t is not None]
    outdir = d / "_read" / src.stem
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)
    for i, (t, s) in enumerate(segs, 1):
        (outdir / f"p{i:02d}.md").write_text(s, encoding="utf-8")
        chars = len(re.sub(r"\s", "", s))
        print(f"p{i:02d}  {chars:>7,}자  {t[:50]}")
    print(f"읽기용 조각: {outdir.relative_to(ROOT)}/ (여기는 읽기 전용. 수정본은 단계별 _pNN 파일로 쓴다)")
    return 0


# ---------------------------------------------------------------------------
# next-round / reset / figures / doctor
# ---------------------------------------------------------------------------
def cmd_next_round(a):
    d = require_active()
    state = load_state(d)
    rec = state.get("reviews", {}).get("10_ensemble-review-2")
    ens = state.setdefault("ensemble", {})
    if not rec:
        raise BookError("10_ensemble-review-2 점수 기록이 없습니다.")
    if rec.get("passed"):
        raise BookError("이미 통과했습니다. 11_draft-final로 진행하세요.")
    if ens.get("round", 2) >= 3:
        raise BookError("합평은 최대 3라운드입니다. 미통과 사유를 남기고 11_draft-final로 진행하세요.")
    b09 = backup(d, stage_file(d, "09_draft-v4"))
    b10 = backup(d, stage_file(d, "10_ensemble-review-2"))
    for p in d.glob("09_draft-v4_p*.md"):
        backup(d, p)
        p.unlink()
    stage_file(d, "09_draft-v4").unlink()
    stage_file(d, "10_ensemble-review-2").unlink()
    log = d / "_changelog.md"
    if log.is_file():
        t = re.sub(r"^## 09_draft-v4\s*$", f"## 09_draft-v4 ({ens.get('round', 2)}라운드, 백업됨)", log.read_text(encoding="utf-8"), flags=re.M)
        log.write_text(t, encoding="utf-8")
    state.get("stages", {}).pop("09_draft-v4", None)
    state.get("stages", {}).pop("10_ensemble-review-2", None)
    log_event(state, "next-round", failed={k: v for k, v in rec.items() if k != "findings"})
    archived = f"10_ensemble-review-2@r{ens.get('round', 2)}"
    state["reviews"][archived] = state["reviews"].pop("10_ensemble-review-2")  # 다음 라운드 채점은 새 리뷰로 본다
    ens["revise_review_id"] = archived
    ens["round"] = ens.get("round", 2) + 1
    ens["revise_base"] = str(b09.relative_to(d))
    ens["revise_review"] = str(b10.relative_to(d))
    save_state(d, state)
    print(f"합평 {ens['round']}라운드 준비 완료. 09_draft-v4를 {ens['revise_base']} 기준, {ens['revise_review']} 지적으로 다시 수정합니다.")
    return 0


def cmd_reset(a):
    d = require_active()
    sid = a.sid
    if sid not in STAGE_BY_ID:
        raise BookError(f"알 수 없는 단계: {sid}")
    state = load_state(d)
    idx = STAGES.index(STAGE_BY_ID[sid])
    removed = []
    for st in STAGES[idx:]:
        for p in [stage_file(d, st["id"])] + sorted(d.glob(f"{st['id']}_p*.md")):
            if p.exists():
                backup(d, p)
                p.unlink()
                removed.append(p.name)
        old = state.get("reviews", {}).pop(st["id"], None)
        if old:
            log_event(state, "reset-review", sid=st["id"], red=old.get("red"), yellow=old.get("yellow"), avg=old.get("avg"))
        state.get("stages", {}).pop(st["id"], None)
    # 되돌린 수정 단계의 변경 로그 섹션은 이름을 바꿔 남긴다 (새 리뷰의 같은 지적 ID에 옛 ✅가 쓰이지 않게)
    log = d / "_changelog.md"
    if log.is_file():
        backup(d, log)
        t = log.read_text(encoding="utf-8")
        for st in STAGES[idx:]:
            if st["kind"] in ("draft", "revise"):
                t = re.sub(rf"^##\s+{re.escape(st['id'])}\s*$", f"## {st['id']} (되돌림 {now()})", t, flags=re.M)
        log.write_text(t, encoding="utf-8")
    out = OUTPUT / d.name
    for name in ("metadata.md", "final.docx", "final.pdf", "build-report.md", "verify-report.md"):
        if (out / name).exists():
            backup(d, out / name)
            (out / name).unlink()
            removed.append(f"output/{name}")
    log_event(state, "reset", sid=sid, removed=removed)
    if idx <= STAGES.index(STAGE_BY_ID["02_outline"]):
        state.get("approvals", {}).pop("outline", None)
    if idx <= STAGES.index(STAGE_BY_ID["03_draft-v1"]):
        state.get("approvals", {}).pop("tone", None)
    if idx <= STAGES.index(STAGE_BY_ID["10_ensemble-review-2"]):
        if state.get("ensemble", {}).get("history"):
            log_event(state, "reset-ensemble", history=state["ensemble"]["history"])
        state["ensemble"] = {} if idx <= STAGES.index(STAGE_BY_ID["08_ensemble-review-1"]) else {"round": 2}
    state.get("approvals", {}).pop("facts", None)
    state.get("approvals", {}).pop("low-score", None)
    save_state(d, state)
    print(f"되돌림: {sid}부터. 백업 후 지운 파일 {len(removed)}개 (_backup/에 있음)")
    return 0


def cmd_figures(a):
    d = require_active()
    text = resolve_doc(d, a.sid).read_text(encoding="utf-8")
    chapter = ""
    for line in text.splitlines():
        if line.startswith("## "):
            chapter = line[3:].strip()
        for m in re.finditer(r"\[그림\s*(\d+)\s*[:.]\s*([^\]]*)\]", line):
            print(f"fig{int(m.group(1)):02d}\t{m.group(2).strip()}\t({chapter[:30]})")
    return 0


KIND = {0: "사내·저자 자료", 1: "공식 자료", 2: "언론·연구", 3: "블로그·SNS"}


def cmd_facts(a):
    """사실 확인 표: 출처가 붙은 문장, 지어낸 예시 수치, 출처 기록과 다른 수치. 위험한 것부터 정렬한다."""
    import verify
    d = require_active()
    ms = stage_file(d, "15_manuscript")
    src = ms if ms.is_file() else max(d.glob("[01][0-9]_draft*.md"), default=None)
    if not src:
        raise BookError("원고가 없습니다.")
    cites = {}
    cpath = d / "01_citations.json"
    if cpath.is_file():
        try:
            cites = {c["id"]: c for c in json.loads(cpath.read_text(encoding="utf-8"))}
        except (ValueError, KeyError, TypeError) as e:
            raise BookError(f"출처 파일 {cpath.name}을 읽지 못했습니다 ({e}). JSON 목록이고 항목마다 id가 있어야 합니다.")
    rows, examples = [], []
    where = ""
    for ln, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        h = re.match(r"^(#{1,2})\s+(.*)", line)
        if h:
            where = h.group(2)[:24]
            continue
        for sent in re.split(r"(?<=[.!?。])\s+", line):
            ids = [x.strip() for c in verify.CITE_RE.findall(sent) for x in c.split(",")]
            nums = [verify.norm_num(m.group(0)) for m in verify.NUMERIC_CLAIM_RE.finditer(sent)]
            text = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\s*\[\[[^\]]*\]\]", "", sent)).strip(" |")[:70].replace("|", "\\|")
            if verify.EXAMPLE_TAG in sent:
                examples.append(f"| {ln} | {where} | {text} |")
            for cid in ids:
                c = cites.get(cid, {})
                tier = c.get("tier", 3)
                claim = str(c.get("claim", ""))
                claim_nums = {verify.norm_num(m.group(0)) for m in verify.NUMERIC_CLAIM_RE.finditer(claim)}
                mismatch = [n for n in nums if n not in claim_nums]
                # '15곳', '3건', '2회'처럼 수치 검사가 세지 않는 수량도, 출처 기록에 숫자가 있으면 숫자끼리 대조한다
                plain = lambda s: {verify.norm_num(x) for x in re.findall(r"\d[\d,.]*", re.sub(r"\[\[[^\]]*\]\]", "", s))}  # noqa: E731
                claim_digits = plain(claim)
                if claim_digits:
                    extra = sorted(plain(sent) - claim_digits - {re.sub(r"\D.*$", "", n) for n in nums if n not in mismatch})
                    mismatch += [x for x in extra if x not in mismatch]
                risk = (2 if mismatch else 0) + (tier if isinstance(tier, int) else 3) / 3
                link = c.get("url") or c.get("path") or ""
                cell = lambda x: str(x).replace("|", "\\|")
                shown = nums + [f"⚠️{x}" for x in mismatch if x not in nums]
                rows.append((risk, f"| {'⚠️ ' if mismatch else ''}{ln} | {cell(where)} | {text} | {', '.join(shown) or '-'} | "
                                   f"{cell(c.get('title', cid))} | {KIND.get(tier, '?')} | {cell(str(c.get('claim', ''))[:60])} | {cell(link)} |"))
    rows.sort(key=lambda r: -r[0])
    out = OUTPUT / d.name / "fact-check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 출판 전 사실 확인 ({src.name})", "",
             "15~30분쯤 걸립니다. 시간이 없으면 ① ⚠️ 표시(출처 기록과 다른 수치) ② 블로그·SNS 출처 ③ 지어낸 예시 수치 "
             "④ 실명일 수 있는 표현과 보안 등급 표시가 있는 자료를 먼저 보세요.", "",
             "## 출처가 붙은 문장 (위험한 것부터)", "",
             "| 행 | 위치 | 문장 | 수치 | 출처 | 종류 | 출처에 기록된 내용 | 링크 |", "|---|---|---|---|---|---|---|---|"]
    lines += [r for _, r in rows] or ["| - | - | 출처가 붙은 문장이 없습니다 | | | | | |"]
    esc = lambda x: str(x).replace("|", "\\|").replace("\n", " ")
    ms_text = src.read_text(encoding="utf-8")
    toc_path, toc_now = load_toc()
    toc_now = toc_now or ""
    figdata = {}
    img = OUTPUT / d.name / "images"
    if (img / "figdata.json").is_file():
        try:
            figdata = json.loads((img / "figdata.json").read_text(encoding="utf-8"))
        except ValueError:
            figdata = {}
    claim_nums = {re.sub(r"[,\s]", "", x).rstrip(".") for c in cites.values() for x in re.findall(r"\d[\d,.]*", str(c.get("claim", "")))}
    lines += ["", "## 그림 속 글자와 수치", "",
              "그림에 들어간 글자와 수치도 책의 내용입니다. 그림을 열어 함께 보고, ⚠️는 출처 기록에 없는 수치이거나 글자 기록이 없는 그림입니다.", "",
              "| 그림 | 캡션 | 그림 속 글자 | 그림 속 수치 |", "|---|---|---|---|"]
    fig_rows = []
    for m in re.finditer(r"\[그림\s*(\d+)\s*[:.]\s*([^\]]*)\]", ms_text):
        n = int(m.group(1))
        key, label = f"fig{n:02d}", f"그림 {n}"
        rec = figdata.get(key)
        png = next((img / f"{key}{e}" for e in (".png", ".jpg") if (img / f"{key}{e}").is_file()), None)
        if not rec or not png or rec.get("sha") != C.sha(png):
            fig_rows.append(f"| ⚠️ {label} | {esc(m.group(2))[:40]} | 글자 기록 없음: 그림 도구 밖에서 만들었거나 다시 저장된 그림입니다. 그림을 열어 직접 확인하세요 | |")
            continue
        if rec.get("has_image"):
            fig_rows.append(f"| ⚠️ {label} | {esc(m.group(2))[:40]} | 캡처 이미지가 들어 있습니다. 이미지 속 글자(이름, 회사명)는 검사기가 읽지 못하니 그림을 열어 확인하세요. "
                            f"기록된 글자: {esc(' / '.join(map(str, rec.get('texts', []))))[:80] or '없음'} | |")
            continue
        texts = rec.get("texts", []) + rec.get("cells", [])
        vals = []
        for v in rec.get("values", []):
            t = f"{v:,}" if isinstance(v, int) else (f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
            vals.append(t if str(v).rstrip("0").rstrip(".") in claim_nums or re.sub(",", "", t) in claim_nums else f"⚠️ {t}")
        warn = "⚠️ " if not texts or any(x.startswith("⚠️") for x in vals) else ""
        fig_rows.append(f"| {warn}{label} | {esc(m.group(2))[:40]} | {esc(' / '.join(map(str, texts))) or '글자 없음'} | {', '.join(vals) or '-'} |")
    lines += fig_rows or ["| - | - | 그림 없음 | |"]

    # 실명일 수 있는 표현: 검사기와 같은 기준(verify.leak_scan)으로 원고 원문, 그림, 출처 목록을 보고, 직함이 붙은 모든 표현을 덧붙인다
    rules = dict(verify.RULE_DEFAULTS)
    rules.update({k: v for k, v in C.parse_kv(C.get_section(toc_now, "검증 규칙"), verify.RULE_DEFAULTS.keys())[0].items() if v})
    sdir = C.input_dir(ROOT) / "sources"
    sfiles = [x for x in sdir.rglob("*") if x.is_file() and not x.name.startswith(".")] if sdir.is_dir() else []
    sfiles += [x for x in [d / "01_research-notes.md"] if x.is_file()]
    P = verify.leak_policy(toc_now, rules, sfiles)
    blobs = [(f"{ln}행", line) for ln, line in enumerate(ms_text.splitlines(), 1)]
    if toc_path:
        blobs += [(f"그림 {int(k[3:])}", t) for k, t in verify.figure_texts(toc_path, src, ms_text)]
    blobs += [(f"출처 {cid}", t) for cid, t in verify.citation_texts(None, src)]
    groups = {}
    rank = {"🔴": "0", "🟡": "1", "ℹ️": "2"}

    def note(expr, where, why):
        g = groups.setdefault(C.despace(expr), {"expr": expr, "where": [], "why": why})
        if where not in g["where"]:
            g["where"].append(where)
        if why < g["why"]:
            g["why"] = why

    name_cats = ("사내 자료에 나온 실명", "사내 자료에 나온 영문 이름·이메일", "실명 후보", "실명으로 보이는 인물", "공인 실명 확인",
                 "실명일 수 있는 호칭", "자료에 부서명과 함께 나온 낱말")
    generic = re.compile(r"^(우리|고객|담당|해당|모든|각|현|전|신임|신규|소속|직속|일반|여러|다른|같은|그|이|저|한|두|세|대표|사내|외부|내부)$"
                         r"|(팀|부|실|사|국|처|청|원|과|소|단|센터|본부|그룹)$|[에의는은이가를을도로와과]$")
    plain = "|".join(t for t in verify.PERSONAL_TITLES.split("|") if t not in ("책임", "리더", "님", "씨"))
    broad = re.compile(r"(?<![가-힣])(?P<pre>[가-힣]{2,4})[ \t]?(?P<title>" + plain + r"|대표|회장|교수|기자|작가|변호사|의원)" + verify.TITLE_END)
    for where, blob in blobs:
        for sev, cat, detail, _ in verify.leak_scan(blob, P):
            if cat in name_cats:
                note(re.sub(r" \(.*$", "", detail).strip("'"), where, f"{rank[sev]} {cat}")
        for m in broad.finditer(blob):
            if not generic.search(m.group("pre")) and C.despace(m.group(0)) not in P["allow"]:
                note(m.group(0), where, "3 직함이 붙은 표현")
    rows_p = sorted(groups.values(), key=lambda g: (g["why"], -len(g["where"])))
    lines += ["", "## 실명일 수 있는 표현", "",
              "실명이면 이름을 빼거나 바꿉니다. 실명이 아니거나 써도 되는 이름(예: 대표이사 인사말)이면 AI에게 '이 이름은 괜찮다'고 말하면 "
              "검증 규칙 '실명 예외'에 넣습니다. 위에서부터 위험한 순서입니다.", "",
              "| 표현 | 근거 | 나온 곳 | 횟수 |", "|---|---|---|---|"]
    lines += [f"| {esc(g['expr'])} | {g['why'][2:]} | {', '.join(g['where'][:5])}{' …' if len(g['where']) > 5 else ''} | {len(g['where'])} |"
              for g in rows_p[:80]] or ["| - | 없음 | | |"]
    if len(rows_p) > 80:
        lines.append(f"| … | 외 {len(rows_p) - 80}개 표현 | 이 파일 끝의 전체 목록 | |")
    if P["src_failed"]:
        lines += ["", f"읽지 못한 사내 자료 {len(P['src_failed'])}개({', '.join(P['src_failed'][:5])})의 이름은 검사기가 대조하지 못했습니다. 그 자료에 나오는 사람 이름이 본문에 없는지 봅니다."]
    graded = [x.name for x in sfiles if re.search(r"대외비|기밀|극비|사내\s*한정|Confidential|Internal\s+only", verify.read_any(x) or "", re.I)]
    if graded:
        lines += ["", "## 보안 등급 표시가 있는 자료", "",
                  f"{', '.join(graded[:10])}. 이 자료에서 온 수치와 사례가 배포 범위에 맞는지 봅니다. 사내 배포 책이면 출판 설정 '배포 표기'를 확인합니다."]
    if P["never"]:
        lines += ["", "## 쓰면 안 되는 것 (저자 자료)", "", f"- {P['never']}",
                  f"- 검사기가 막는 낱말: {', '.join([w for w, _ in P['forbidden']] + P['never_terms']) or '없음'} (띄어쓰기, 대소문자, '-'·'_'·'·'는 무시합니다)",
                  "- 위 목록에 없는 영문명, 약칭, 범주(예: '고객사 이름')는 검사기가 알 수 없습니다. 본문과 그림에 그런 표현이 없는지 사람이 봅니다."]
    lines += ["", "## 지어낸 예시 수치 ([[예시]] 표시)", "", "| 행 | 위치 | 문장 |", "|---|---|---|"]
    lines += examples or ["| - | - | 없음 |"]
    if len(rows_p) > 80:
        lines += ["", "## 실명일 수 있는 표현 (전체)", ""] + [f"- {esc(g['expr'])}: {', '.join(g['where'])}" for g in rows_p[80:]]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = load_state(d)
    state["facts_table"] = {"ms": C.sha(src), "evidence": evidence_sha(d), "toc": C.sha(toc_now), "file": C.sha(out)}
    save_state(d, state)
    flagged = sum(1 for r in rows if r[0] >= 2)
    print(f"사실 확인 표: {out.relative_to(ROOT)} (출처 문장 {len(rows)}개, 출처 기록과 다른 수치 {flagged}개, 예시 수치 {len(examples)}개)")
    return 0


def cmd_export(a):
    d = require_active()
    dest = pathlib.Path(a.to) if a.to else ROOT
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / f"book-work-{d.name}-saved-{datetime.datetime.now().strftime('%m%d_%H%M%S')}.zip"
    state = load_state(d)
    log_event(state, "export", file=zpath.name)
    save_state(d, state)
    count = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for base in (C.input_dir(ROOT), d, OUTPUT / d.name):
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if (f.is_file() and "_read" not in f.parts and f.name != ".DS_Store" and f.suffix != ".zip"
                        and f.resolve() != zpath.resolve()):
                    z.write(f, f.relative_to(ROOT).as_posix())
                    count += 1
        z.writestr("draft/.active", d.name + "\n")
    print(f"작업 묶음: {zpath} (파일 {count}개)")
    print("사용자 안내: 이 파일을 내려받아 두세요. 다음 대화에서 가장 최근 파일 하나만 올리고 '책 이어서 써줘'라고 하면 이어집니다.")
    return 0


def _legacy_name(n):
    """4.0 이전 작업 묶음의 book/ 경로를 user_input/으로 바꾼다."""
    return C.INPUT_DIR + n[len(C.LEGACY_INPUT_DIR):] if n.startswith(C.LEGACY_INPUT_DIR + "/") else n


def cmd_import(a):
    zpath = pathlib.Path(a.zip)
    if not zpath.is_file():
        raise BookError(f"{zpath}이 없습니다.")
    toc_rel = f"{C.INPUT_DIR}/{C.TOC_NAME}"
    with zipfile.ZipFile(zpath) as z:
        raw = z.namelist()
        names = {_legacy_name(n): n for n in raw}  # 풀 경로 → ZIP 안의 이름 (예전 book/ 묶음도 user_input/으로 푼다)
        bad = [n for n in raw if n.startswith("/") or "\\" in n or ":" in n or ".." in pathlib.PurePosixPath(n).parts
               or not _legacy_name(n).startswith((C.INPUT_DIR + "/", "draft/", "output/"))]
        if bad:
            raise BookError(f"작업 묶음이 아닌 파일이 들어 있습니다: {', '.join(bad[:5])}")
        book_name = z.read("draft/.active").decode("utf-8").strip() if "draft/.active" in names else ""
        if not BOOK_DIR_RE.match(book_name):
            raise BookError("작업 묶음에 책 폴더 정보(draft/.active)가 없습니다. 'book.py export'로 만든 파일인지 확인하세요.")
        if (DRAFT / book_name).exists():
            raise BookError(f"draft/{book_name}이 이미 있습니다. 덮어쓰지 않습니다. 다른 작업 폴더에서 불러오세요.")
        # 이 책 폴더 밖의 draft/output 경로나, 이미 있는 파일은 하나도 덮어쓰지 않는다
        foreign = [n for n in names if n.startswith(("draft/", "output/")) and n != "draft/.active"
                   and pathlib.PurePosixPath(n).parts[1] != book_name]
        exists = [n for n in names if not n.endswith("/") and n != "draft/.active" and n != toc_rel and (ROOT / n).exists()]
        if foreign or exists:
            raise BookError(f"작업 묶음이 다른 책의 파일이나 이미 있는 파일을 덮어쓰려 합니다: {', '.join((foreign + exists)[:5])}")
        toc_here = C.input_dir(ROOT) / C.TOC_NAME
        if toc_here.is_file() and toc_rel not in names:
            raise BookError("작업 묶음에 책 설정이 없는데 이 작업 폴더에는 다른 책 설정이 있습니다. 빈 작업 폴더에서 불러오세요.")
        if toc_here.is_file() and toc_rel in names and z.read(names[toc_rel]) != toc_here.read_bytes():
            raise BookError("이 작업 폴더의 책 설정이 작업 묶음의 것과 다릅니다. 다른 책이면 빈 작업 폴더에서 불러오세요.")
        for n, orig in names.items():
            if n.endswith("/") or n == "draft/.active" or (n == toc_rel and toc_here.is_file()):
                continue
            target = ROOT / n
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(orig))
    (DRAFT / ".active").write_text(book_name + "\n", encoding="utf-8")
    d = DRAFT / book_name
    state = load_state(d)
    log_event(state, "import", file=zpath.name)
    save_state(d, state)
    print(f"불러오기 완료: draft/{book_name}/. 'book.py status'로 이어서 할 일을 확인하세요.")
    return 0


def cmd_doctor(a):
    ok = True
    voice = ""
    toc_path, toc = load_toc()
    print(f"책 설정 파일: {toc_path or '없음'}")
    if not toc:
        print("  ✗ user_input/user-book-toc.md가 없습니다 (작업 폴더 → 스킬 폴더 순으로 찾음)")
        ok = False
    else:
        ph = C.PLACEHOLDER_RE.findall(toc)
        if ph:
            print(f"  ✗ 채우지 않은 칸 {len(ph)}개: {', '.join(sorted(set(ph))[:6])}")
            ok = False
        info = C.basic_info(toc)
        for k in ("제목", "필명", "대상 독자", "목표 분량"):
            if not info.get(k):
                print(f"  ✗ 기본 정보 '{k}' 없음")
                ok = False
        if not C.target_chars(toc):
            print("  ✗ 목표 분량에 'N~M자' 형식이 없습니다 (분량 검사 불가)")
            ok = False
        struct = C.toc_structure(toc)
        print(f"  목차: 부 {len(struct)}개, 장 {sum(len(c) for _, c in struct)}개")
        if not struct:
            ok = False
        author = C.get_section(toc, "저자 자료") or ""
        m = re.search(r"화자 성격:\s*([^\n#]+)", author)
        voice = m.group(1).strip() if m else ""
        if voice not in ("실제 저자", "가상 화자", "조직 화자"):
            print(f"  ✗ 저자 자료의 '화자 성격'은 실제 저자, 가상 화자, 조직 화자 중 하나 (지금: '{voice}')")
            ok = False
        from verify import RULE_DEFAULTS
        from build_docx import PUB_DEFAULTS
        for sec, keys in (("검증 규칙", RULE_DEFAULTS.keys()), ("출판 설정", PUB_DEFAULTS.keys())):
            for k in C.parse_kv(C.get_section(toc, sec), keys)[1]:
                print(f"  ✗ {sec}에 알 수 없는 키: '{k}'")
                ok = False
    src = C.input_dir(ROOT) / "sources"
    n = len([p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")]) if src.is_dir() else 0
    print(f"  사내·저자 자료 (user_input/sources/): {n}개 파일")
    if toc and n == 0 and voice == "조직 화자":
        print("  ✗ 조직 화자는 사례를 사내 자료에서만 가져옵니다. user_input/sources/에 자료를 넣으세요")
        ok = False
    hwp = [p.name for p in src.rglob("*.hwp*")] if src.is_dir() else []
    if hwp:
        print(f"  ✗ 한글(HWP) 파일은 읽지 못합니다. PDF로 저장해 넣으세요: {', '.join(hwp[:5])}")
        ok = False
    for mod in ("docx", "matplotlib"):
        try:
            __import__(mod)
            print(f"  ✓ 파이썬 모듈 {mod}")
        except ImportError:
            print(f"  ✗ 파이썬 모듈 {mod} 없음: pip install -r \"{C.SKILL_DIR / 'scripts' / 'requirements.txt'}\"")
            ok = False
    print(f"  {'✓' if shutil.which('soffice') else '·'} LibreOffice (선택: 있으면 PDF 쪽수를 실측한다)")
    print("점검 결과:", "이상 없음" if ok else "위 ✗ 항목을 고치세요")
    sdir = C.input_dir(ROOT) / "sources"
    if sdir.is_dir():
        import verify
        files = [x for x in sdir.rglob("*") if x.is_file() and not x.name.startswith(".")]
        graded = [x.name for x in files if re.search(r"대외비|기밀|극비|사내\s*한정|Confidential|Internal\s+only", verify.read_any(x) or "", re.I)]
        unread = [x.name for x in files if verify.read_any(x) is None]
        print(f"사내 자료: {len(files)}개 (user_input/sources/)")
        if graded:
            print(f"  ! 보안 등급 표시가 있는 자료: {', '.join(graded[:8])}. 이 자료는 AI 서비스로 전송됩니다. 회사 정책에 맞는지 사용자에게 확인하세요.")
        if unread:
            print(f"  ! 읽지 못하는 자료: {', '.join(unread[:8])}. PDF(글자가 있는 PDF), Word, 텍스트로 저장해 다시 넣게 하세요.")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="책 작업 상태 관리 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("init"); p.add_argument("--date"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("use"); p.add_argument("name"); p.set_defaults(fn=cmd_use)
    p = sub.add_parser("doctor"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("config"); p.add_argument("pairs", nargs="+"); p.set_defaults(fn=cmd_config)
    p = sub.add_parser("approve"); p.add_argument("what", choices=list(APPROVAL_TARGET)); p.add_argument("--note"); p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("accept-toc"); p.set_defaults(fn=cmd_accept_toc)
    p = sub.add_parser("complete"); p.add_argument("sid"); p.set_defaults(fn=cmd_complete)
    p = sub.add_parser("score"); p.add_argument("sid"); p.add_argument("--reason"); p.set_defaults(fn=cmd_score)
    p = sub.add_parser("merge"); p.add_argument("sid"); p.set_defaults(fn=cmd_merge)
    p = sub.add_parser("split"); p.add_argument("sid"); p.set_defaults(fn=cmd_split)
    p = sub.add_parser("next-round"); p.set_defaults(fn=cmd_next_round)
    p = sub.add_parser("reset"); p.add_argument("sid"); p.set_defaults(fn=cmd_reset)
    p = sub.add_parser("figures"); p.add_argument("sid"); p.set_defaults(fn=cmd_figures)
    p = sub.add_parser("facts"); p.set_defaults(fn=cmd_facts)
    p = sub.add_parser("export"); p.add_argument("--to"); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("import"); p.add_argument("zip"); p.set_defaults(fn=cmd_import)
    args = ap.parse_args(argv)
    global ROOT, DRAFT, OUTPUT
    ROOT = pathlib.Path.cwd()
    DRAFT, OUTPUT = ROOT / "draft", ROOT / "output"
    try:
        return args.fn(args)
    except BookError as e:
        print(f"중단: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
