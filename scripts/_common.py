"""book-publishing 스크립트 공통 모듈.

verify.py, book.py, build_docx.py, figure_kit.py가 함께 쓰는 기능만 둔다.
- 경로 해석: 스킬 폴더(SKILL_DIR)와 작업 폴더(cwd)를 구분한다
- 책 설정 파일(user-book-toc.md) 위치 찾기
- 마크다운 섹션 추출 (같은 레벨 이하의 다음 헤딩까지)
- "키: 값" 규칙 파싱과 알 수 없는 키 감지
- 목차에서 기대 구조(부, 장) 뽑기
"""
import hashlib
import re
import pathlib

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent  # 스킬 폴더 (저장소 루트)
INPUT_DIR = "user_input"                                     # 사용자가 넣는 파일: 책 설정과 sources/(사내 자료)
LEGACY_INPUT_DIR = "book"                                    # 4.0 이전 작업 폴더의 입력 폴더
TOC_NAME = "user-book-toc.md"
TOC_TEMPLATE = SKILL_DIR / INPUT_DIR / TOC_NAME              # 스킬에 든 빈 템플릿
PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
def input_dir(root=None):
    """작업 폴더의 입력 폴더. user_input/이 없고 예전 book/만 있으면 그것을 쓴다."""
    root = pathlib.Path(root) if root else pathlib.Path.cwd()
    new, old = root / INPUT_DIR, root / LEGACY_INPUT_DIR
    return old if not new.exists() and (old / TOC_NAME).is_file() else new


def find_toc(root=None):
    """작업 폴더의 user_input/user-book-toc.md를 먼저, 없으면 스킬의 빈 템플릿을 돌려준다.

    템플릿에는 빈칸이 있으므로 status가 '첫 실행 인터뷰'로 안내한다. 사용자의 책 정보는 늘
    작업 폴더에 두어, 스킬을 업데이트해도 덮어써지지 않게 한다.
    """
    root = pathlib.Path(root) if root else pathlib.Path.cwd()
    for cand in (input_dir(root) / TOC_NAME, TOC_TEMPLATE):
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# 마크다운 섹션
# ---------------------------------------------------------------------------
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def iter_headings(md):
    """(줄 번호(0부터), 레벨, 제목) 목록. 코드 블록 안의 #은 헤딩이 아니다."""
    out = []
    in_code = False
    for i, line in enumerate(md.splitlines()):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def get_section(md, title):
    """제목이 title인 헤딩 아래 본문을 돌려준다.

    끝은 '같은 레벨 이하'의 다음 헤딩이다. 하위 헤딩(###)에서 끊지 않는다.
    (이 규칙을 어겨 목차 검사가 꺼져 있던 회귀가 있었다.)
    섹션이 없으면 None.
    """
    lines = md.splitlines()
    heads = iter_headings(md)
    norm = despace(title)
    for idx, (ln, level, text) in enumerate(heads):
        if despace(text) == norm:
            end = len(lines)
            for ln2, level2, _ in heads[idx + 1:]:
                if level2 <= level:
                    end = ln2
                    break
            return "\n".join(lines[ln + 1:end])
    return None


def despace(s):
    return re.sub(r"\s+", "", s or "")


# ---------------------------------------------------------------------------
# "키: 값" 규칙
# ---------------------------------------------------------------------------
def parse_kv(section_text, known_keys):
    """'- 키: 값' 줄을 읽어 (값 사전, 알 수 없는 키 목록)을 돌려준다.

    - 값 뒤 '  # 주석'은 제거한다
    - 키 목록은 첫 '- 키: 값' 줄부터 이어지는 목록 하나다. 목록이 시작된 뒤
      목록이 아닌 줄(예: '작성 요령:')을 만나면 거기서 멈춘다. 그 아래 설명 목록은 키가 아니다
    - known_keys에 없는 키는 오타 가능성이 있으므로 따로 돌려준다
    """
    values, unknown = {}, []
    if not section_text:
        return values, unknown
    started = False
    for raw in section_text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith("- "):
            if started:
                break
            continue
        started = True
        line = raw[2:].strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip("`")
        if len(key) > 40:
            continue
        val = re.split(r"\s+#\s", val, maxsplit=1)[0].strip()
        if key in known_keys:
            values[key] = val
        else:
            unknown.append(key)
    return values, unknown


# 문단 안 강조와 태그를 나누는 규칙. build_docx(변환)와 verify(사전 검사)가 같은 규칙을 써서
# 검사는 통과했는데 변환에서 기호가 남는 일이 없게 한다.
INLINE_SPLIT_RE = re.compile(r"(\*\*\*[^*]+\*\*\*|\*\*(?:[^*]|\*[^*\s][^*]*\*)+\*\*|(?<![\w_.])__[^_\s][^_]*__(?![\w_]|\.\w)|`[^`]+`"
                             r"|\[\[[^\]]+\]\]|<br\s*/?>|(?<![*\w])\*[^*\s][^*]*\*(?!\*))")
def citation_line(c):
    """책 끝 출처 목록의 한 줄 (번호 제외). build_docx가 찍고 verify가 같은 글을 검사한다."""
    line = str(c.get("title") or c.get("id", ""))
    if c.get("url"):
        line += f". {c['url']}"
    return line


PAPER = {"B5": (176, 250), "A5": (148, 210), "A4": (210, 297), "신국판": (152, 225)}  # mm


def paper_mm(spec):
    """'A4', '176x250' 같은 판형 값을 (가로, 세로) mm로. 모르면 B5."""
    spec = (spec or "").strip()
    if spec in PAPER:
        return PAPER[spec]
    m = re.match(r"(\d+)\s*[x×]\s*(\d+)", spec)
    return (int(m.group(1)), int(m.group(2))) if m else PAPER["B5"]


def parse_pairs(spec):
    """'A → B; C → D'를 [(A, B), ...]로. 화살표는 →, ->, ⇒ 모두 허용."""
    pairs = []
    if not spec or spec.strip() in ("없음", "-", "none"):
        return pairs
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = re.split(r"\s*(?:→|->|⇒)\s*", chunk, maxsplit=1)
        wrong = parts[0].strip().strip('"“”')
        right = parts[1].strip().strip('"“”') if len(parts) > 1 else ""
        if wrong:
            pairs.append((wrong, right))
    return pairs


def is_yes(val):
    return (val or "").strip().lower() in ("예", "yes", "y", "true", "허용", "on", "1")


# ---------------------------------------------------------------------------
# 기본 정보
# ---------------------------------------------------------------------------
def basic_info(toc):
    sec = get_section(toc, "기본 정보") or ""
    info = {}
    for raw in sec.splitlines():
        if raw.startswith("- ") and ":" in raw:
            k, _, v = raw[2:].partition(":")
            info[k.strip()] = v.strip()
    return info


def target_chars(toc):
    """'목표 분량'에서 'N~M자'를 찾아 (N, M). 없으면 None."""
    val = basic_info(toc).get("목표 분량", "")
    m = re.search(r"([\d,]+)\s*[~∼-]\s*([\d,]+)\s*자", val)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


# ---------------------------------------------------------------------------
# 목차 구조
# ---------------------------------------------------------------------------
# 장 라벨로 인정하는 형식. 이 형식으로 시작하는 목록 줄만 '장'으로 보고,
# 나머지 목록 줄은 작성 지시로 본다. (예: "- 제 1장. 제목", "- [활용법 01] 제목")
# 대괄호 라벨은 번호가 있어야 장이다 ([활용법 01], [실습 3]). [주의], [Tip], [그림 3: …]은 장이 아니다.
# '3장 분량으로'처럼 숫자로 시작하는 작성 지시와 [표 1], [Tip 1] 같은 주석 라벨은 장이 아니다.
CHAPTER_LABEL_RE = re.compile(
    r"^(제\s*\d+\s*장|\[(?!\s*(?:그림|표|도표|그래프|사진|자료|출처|Tip|TIP|tip|팁|예시|예|주의|참고|사례|Q|질문|NOTE|Note|Figure|Fig|Table|\d))[^\]\d]{1,15}\d+\]|부록\s*[A-Z0-9](?=[\s:.])|Chapter\s*\d+)"
)


def split_label(title):
    """'제1부: 고수는…' → ('제1부', '고수는…'). 콜론이나 마침표 앞을 라벨로 본다."""
    t = re.sub(r"<br\s*/?>", " ", title).strip()
    m = CHAPTER_LABEL_RE.match(t)
    if m:
        label = m.group(1)
        rest = t[m.end():].lstrip(" .:").strip()
        return label, rest
    if ":" in t:
        a, _, b = t.partition(":")
        return a.strip(), b.strip()
    return t, ""


def toc_structure(toc):
    """목차 섹션에서 [(부 제목, [장 제목, ...]), ...]을 뽑는다.

    - 부(division): 목차 섹션 안의 ### 헤딩 (프롤로그, 제N부, 에필로그, 부록 등)
    - 장(chapter): 부 아래 최상위 목록 줄 중 CHAPTER_LABEL_RE로 시작하는 줄
    """
    sec = get_section(toc, "목차")
    if sec is None:
        return []
    divisions = []
    for line in sec.splitlines():
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) >= 3:
            divisions.append((m.group(2).strip(), []))
            continue
        if not divisions:
            continue
        if line.startswith("- "):
            item = line[2:].strip()
            if CHAPTER_LABEL_RE.match(item):
                divisions[-1][1].append(item)
    return divisions


def manuscript_divisions(md):
    """원고를 '# ' 헤딩 기준으로 나눈다. [(제목 또는 None, 텍스트)]. 첫 '#' 앞은 제목 None."""
    lines = md.splitlines(keepends=True)
    segs = []
    cur_title, cur = None, []
    in_code = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        if not in_code and line.startswith("# "):
            if cur or cur_title is not None:
                segs.append((cur_title, "".join(cur)))
            cur_title, cur = line[2:].strip(), [line]
            continue
        cur.append(line)
    if cur or cur_title is not None:
        segs.append((cur_title, "".join(cur)))
    return segs


MARKER_RE = re.compile(r"<!--\s*STAGE_COMPLETE:\s*([\w.-]+)\s*-->")


def strip_markers(text):
    return MARKER_RE.sub("", text)


def sha(path_or_text):
    """파일(경로) 또는 문자열의 짧은 해시. 리뷰가 어떤 원고를 보고 썼는지 기록하는 데 쓴다."""
    if isinstance(path_or_text, pathlib.Path):
        if not path_or_text.is_file():
            return None
        data = path_or_text.read_bytes()
    else:
        data = path_or_text.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:12]
