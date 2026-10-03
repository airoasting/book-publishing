#!/usr/bin/env python3
"""원고 자동 검사기. /publish 1단계와 각 수정 단계 직후에 실행한다.

사용법:
  python3 verify.py <원고.md> [--toc 경로] [--report 경로] [--citations 경로] [--min-score 9]

- --toc를 생략하면 작업 폴더 → 스킬 폴더 순으로 user_input/user-book-toc.md를 찾는다
- --citations를 생략하면 원고와 같은 폴더의 01_citations.json을 쓴다 (없으면 출처 검사 생략)
- 종료 코드: 0 통과, 1 🔴 있음, 3 🔴은 없지만 점수가 기준 미만, 2 사용법 오류

책마다 달라지는 기준은 user-book-toc.md의 "## 검증 규칙" 섹션에서 읽는다.
섹션이나 항목이 없으면 중립 기본값을 쓰고, 알 수 없는 키는 🟡로 알린다 (오타가 조용히
기본값으로 돌지 않게 하기 위해서다).

이 점수는 '규칙 준수 점수'다. 내용의 좋고 나쁨은 /review의 리뷰어가 판단한다.
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# 기본값은 user_input/user-book-toc.md 템플릿의 '검증 규칙' 값과 같게 유지한다.
RULE_DEFAULTS = {
    "어미": "해라체",            # 해라체 | 합쇼체 | 해요체 | 없음(검사 안 함)
    "em dash 허용": "아니오",
    "불릿 기호": "•",
    "금지 용어": "없음",
    "금지 영어 표현": "없음",
    "부 브릿지 문구": "N부를 마치며",  # 없음이면 검사 안 함
    "수치 출처 필수": "예",       # 예: 🟡 | 엄격: 🔴 | 아니오
    "장마다 그림 필수": "예",
    "그림 제외 부": "부록, 참고문헌",
    "실명 금지": "예",            # 예: 실명 후보는 사실 확인 표로(ℹ️), 사내 자료에 나온 이름은 🟡 | 엄격: 🔴(공인 직함은 🟡) | 아니오
    "실명 예외": "없음",          # 실명이 아닌데 걸리는 표현. 쉼표로 나열 (예: 차정원 팀장, 소설 속 인물)
}

ALLOWED_COMMENTS = re.compile(r"<!--\s*(STAGE_COMPLETE:[^>]*|box-start|box-end|pagebreak)\s*-->")
CITE_RE = re.compile(r"\[\[\s*(cite_[\w-]+(?:\s*,\s*cite_[\w-]+)*)\s*\]\]")
FIG_RE = re.compile(r"\[그림\s*(\d+)\s*[:.]\s*([^\]]*)\]")
EXAMPLE_TAG = "[[예시]]"
# 출처가 필요한 수치: 금액, 비율, 배수, 사람 수, 순위, 큰 수량. 연도·시간·쪽수 같은 단순 수량은 제외한다.
_N = r"(?<![제\d,.])\d[\d,.]*\s*(?:[백천만억조]\s*\d[\d,.]*\s*)*"  # '1만 5천 원', '2천5백 명'은 한 수치
NUMERIC_CLAIM_RE = re.compile(
    _N + r"[백천만억조]{0,3}\s*(?:여\s*)?(?:%p?|퍼센트|배(?!치|포|송|경|열|달|우|너|속|수|율)|명(?!령|칭|함|단|확|시|예|암|성|소)"
    r"|달러|유로|파운드|위안(?![에의])|엔(?!진|터|딩)|원(?!칙|리|인|형|소|래|론|색|본|문|작|고|화)|위(?!안|해|치|기|험|원|반|협|대|생|주|성))"
    r"|" + _N + r"[백천만]{0,2}\s*(?:억|조(?=\s?(?:원|달러|위안|엔|규모|대|$)|[.,]|[을를이가은는의에도와과]))"
    r"|(?<![가-힣])수[십백천]?[만억조]\s*(?:원|달러|명)"
    r"|\d+\s*분의\s*\d+"
    r"|" + _N + r"[백천]?\s*만(?!\s?(?:자|보|단어|글자))(?:\s*(?:건|개|회|대|곳|가구|부))?(?:(?![가-힣])|(?=[을를이가은는의에도과와]))"
    r"|(?<![가-힣])(?:두|세|네|다섯|열)\s?배(?!치|포|송|경|열|달|우|너|속|추|꼽)"
    r"|[$₩€£¥]\s?\d[\d,.]*|\b(?:USD|KRW|EUR|JPY)\s?\d[\d,.]*|\d[\d,.]*\s?(?:USD|KRW|EUR|JPY)(?![A-Za-z])"
)
# '100% 정확하다는 보장은 없다', '0%는 아니다' 같은 부정 수사는 사실 주장이 아니다
RHETORIC_RE = re.compile(r"(?<!\d)(?:0|100)\s*%.*(?:아니|없|않|못|보장)")
RHETORIC_TOKENS = {"0%", "100%", "0원"}  # 수사 문장에서도 이 둘만 빼고 나머지 수치는 검사한다
SURNAMES = "김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진나엄채원천방공현함변염여추도석선설길연위표명기반왕금옥육인맹제모탁국은편용예봉경마소지"
# 사내 직함·호칭(동료 실명일 수 있다)과 공인 직함(공인은 책에 나올 수 있으나 출처를 확인)을 나눠 둔다.
# 심각도는 검증 규칙 '실명 금지'가 정한다: 예면 사실 확인 표로 넘기고(ℹ️), 엄격이면 사내 직함 🔴·공인 🟡
PERSONAL_TITLES = ("사원|주임|대리|과장|차장|부장|팀장|실장|본부장|센터장|파트장|이사|상무|전무|부사장|사장|매니저|책임연구원|수석연구원|선임연구원"
                   "|책임|선임|수석|선생님|선생|연구원|그룹장|소장|리더|디자이너|개발자|주무관|프로|PM|PD|TL|님|씨")
PUBLIC_TITLES = "대표|회장|의장|원장|교수|박사|선수|감독|기자|작가|변호사|의원|시장|대통령|총리|장관|위원장|위원|배우|여사|총재|검사"
# 직함 뒤는 낱말이 끝나야 한다 ('대리석', '과장하고', '씨앗', '배우기', '리더십'은 직함이 아니다)
TITLE_END = r"(?=[^가-힣]|$|[이가은는을를의께에과와도랑만님한])"
TITLES = PERSONAL_TITLES + "|" + PUBLIC_TITLES
# '김부장님'처럼 성+직함(+님)은 이름이 아니다
TITLE_AS_NAME = "(?!(?:부장|과장|차장|팀장|대리|사장|이사|실장|주임|사원|상무|전무|원장|교수|회장|의장|기자|작가|선생|감독)(?:님|$|[^가-힣]))"
NOT_NAMES = (r"(?!이번|이후|이전|이제|이미|정리|정말|조금|한번|오늘|전체|기존|신규|담당|해당|각자"
             r"|경영진|임원진|운영진|의료진|제작진|연구진|출연진|임직원|교직원|연구원|공무원|회사원|상담원|은행원|조합원"
             r"|한국인|외국인|정치인|직장인|경영인|일반인|방송인|연예인|지식인|최우수|박사급|서울시|부산시|고성능|한정판"
             r"|이렇게|오히려|차라리|조만간|정말로|조용히|신속히|한동안|정확히|최소한|최대한|우선은|함부로|고스란|천천히|서둘러"
             r"|모바일|기업용|전자책|배달앱|성장세|정부안|이번에|이미지|오늘은|하지만|그렇게|그러나|그래서"
             r"|노동자|소비자|투자자|구태여|고용주|오래전|기술원|문화원|진흥원|인재원|국정원|연구소|오리온|장기화|양성한|육성한|차세대|도무지|고강도|장애인|경제인|변호인|이주민|유치원|원자재|현장형|우수성|전경련|구청장|신청자|사용자|관리자|담당자|운영자|기획자|개발자|창업자|진행자|구성원|한창인|정해진|조성한|구성한|편성한)")
# 이름 판정은 '이름에 흔히 쓰는 음절' 목록으로 한다. 이름 두 글자가 모두 이 목록에 있어야 이름이다.
# '반드시', '선배인', '차세대', '한국어'처럼 이름에 잘 쓰지 않는 음절이 끼면 자동으로 빠진다
# (낱말 예외를 하나씩 늘리는 방식은 같은 부류의 새 표현을 막지 못했다).
GIVEN_SYL = ("가강건경고광교구규균근금기길나남다단담대덕도동두라란람량려련렬령록룡루류륜률름리린림"
             "래만명모무문미민범병보복봉빈상서석선설섭성세소송솔수숙순술숭슬승시식신아안애양언엽연열영예여은의이인헌검"
             "옥온완용우욱운웅원월위유윤율을음익일임자장재전정제조종주준중지진찬창채천철청초춘충치"
             "태택표하해혁현형혜호홍화환황회효후훈휘흥희늘결솜누")  # '학', '부', '국'은 뺐다 ('경영학 교수', '경제부 기자', '신흥국 시장')
# 끝 글자 '은'과 '한'은 조사·관형형('조직은', '현명한')과 겹치므로, 흔한 이름 조합('하은', '성한')일 때만 인정한다
GIVEN_EUN_HAN = r"(?:[하다지서예가나채소세혜시유수주]은|성한)"  # '고민은', '이윤은', '연재한'처럼 조사·관형형과 겹치는 조합은 뺐다
GIVEN_OTHER = (r"[" + GIVEN_SYL + r"](?![는은이가을를의에와과도로만한적직])(?!형\s?(?:시장|대표|상품))[" + GIVEN_SYL + r"]")
_NAME = (r"(?P<name>(?:남궁|황보|제갈|선우|독고|[" + SURNAMES + r"])" + TITLE_AS_NAME
         + r"(?:" + GIVEN_EUN_HAN + r"|" + GIVEN_OTHER + r"))")
# 기관 약칭 + 기관장 직함은 이름이 아니다 ('연세대 교수', '공정위 위원장', '조선소 사장', '강남구 대표').
# 끝 글자가 같은 실명('김영구 부장', '박성대 팀장')은 그대로 잡는다
INSTITUTION = r"(?![가-힣]{2}[대위소구][ \t]?(?:교수|총장|학장|위원장|원장|사장|대표|의원|소장|이사장|구청장)" + TITLE_END + ")"
# '김민수 과장', '송태민(과장)', '문재인 전 대통령'. 줄바꿈은 넘지 않는다
REAL_NAME_RE = re.compile(r"(?<![가-힣])" + NOT_NAMES + INSTITUTION + _NAME
                          + r"(?P<gap>[ \t]?\(?)(?:(?:전|현)[ \t])?(?P<title>" + TITLES + r")" + TITLE_END)
# '팀장 김민수는'처럼 직함이 앞에 오는 경우 (사내 직함만)
TITLE_FIRST_RE = re.compile(r"(?<![가-힣])(?P<title>" + PERSONAL_TITLES.replace("|님|씨", "") + r")[ \t]+" + NOT_NAMES
                            + _NAME + r"(?=[^가-힣]|$|[이가은는을를의께에과와도])")
NAME_PARTICLES = "이가은는을를의께에과와도랑님씨"


def name_candidates(blob, allow, with_title=False):
    """실명일 수 있는 표현: [(이름 위치, 표현, 이름, 종류)]. 종류: personal(사내 직함), public(공인 직함), honorific('지원자님'처럼 붙여 쓴 님·씨).

    실명 예외는 표현 전체나 이름이 정확히 같을 때만 뺀다."""
    out = []
    for rx in (REAL_NAME_RE, TITLE_FIRST_RE):
        for m in rx.finditer(blob):
            name = C.despace(m.group("name"))
            if C.despace(m.group(0)) in allow or name in allow:
                continue
            title = m.group("title")
            kind = "public" if re.fullmatch(PUBLIC_TITLES, title) else \
                "honorific" if title in ("님", "씨") and not m.groupdict().get("gap") else "personal"
            row = (m.start("name"), m.group(0).strip(), name, kind)
            out.append(row + (title,) if with_title else row)
    return sorted(out)


def _office_text(path):
    """Word, PowerPoint, Excel 파일의 글을 표준 라이브러리로 읽는다 (본문, 표 안의 표, 머리글, 바닥글, 각주, 슬라이드, 시트)."""
    import html
    import zipfile
    parts = {".docx": r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$",
             ".pptx": r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$",
             ".xlsx": r"xl/(sharedStrings|worksheets/sheet\d+)\.xml$"}[path.suffix.lower()]
    out = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(z.namelist()):
            if re.match(parts, n):
                xml = z.read(n).decode("utf-8", errors="ignore")
                xml = re.sub(r"</w:p>(\s*</w:tc>)", r"\1", xml)  # 칸의 마지막 문단은 줄을 바꾸지 않는다 ('| 정민호 | 팀장 |'이 한 줄로)
                xml = re.sub(r"</(?:w:p|a:p|si|row|w:tr)>", "\n", xml)
                xml = re.sub(r"</(?:w:tc|c|a:tc)>", " | ", xml)
                out.append(html.unescape(re.sub(r"<[^>]+>", "", xml)))
    return "\n".join(out)


_READ_CACHE = {}


def read_any(path):
    """자료 파일의 글 (NFC로 맞춘다). 텍스트(BOM, UTF-8, CP949), PDF(pdftotext가 있으면), Word·PowerPoint·Excel. 못 읽으면 None."""
    import codecs
    import unicodedata
    path = pathlib.Path(path)
    key = (str(path), path.stat().st_mtime if path.exists() else 0)
    if key in _READ_CACHE:
        return _READ_CACHE[key]
    suf = path.suffix.lower()
    text = None
    try:
        if suf in (".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm"):
            raw = path.read_bytes()
            if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                text = raw.decode("utf-16")
            else:
                for enc in ("utf-8-sig", "cp949"):
                    try:
                        text = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
        elif suf == ".pdf":
            import shutil
            import subprocess
            if shutil.which("pdftotext"):
                text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60).stdout
        elif suf in (".docx", ".pptx", ".xlsx"):
            text = _office_text(path)
    except Exception:  # noqa: BLE001 (자료 하나를 못 읽어도 검사는 계속한다)
        text = None
    text = unicodedata.normalize("NFC", text) if text is not None else None
    _READ_CACHE[key] = text
    return text


_SRC_TITLES = "|".join(t for t in PERSONAL_TITLES.split("|") if t not in ("님", "씨"))
# 자료 속 이름은 여러 모양으로 나온다: '박지훈 팀장', '최유리(팀장)', '박지훈, 팀장', '| 강다은 | 대리 |'
SOURCE_NAME_RE = re.compile(r"(?<![가-힣])" + NOT_NAMES + _NAME + r"[ \t]*[(,|/·][ \t]*(?:" + _SRC_TITLES + r")" + TITLE_END)
# 사람 목록: '참석자: 박지훈, 최유리, 윤도현', '작성자: 오세린'. 목록 안에서는 이름 음절 제한을 풀고 성씨만 본다
PEOPLE_LIST_RE = re.compile(r"(?:참석자|참석|작성자|작성|담당자|검토자|검토|발표자|보고자|면담자|수신|참조|배석|결재)\s*[:：]\s*([^\n]+)")
LIST_NAME_RE = re.compile(r"(?:남궁|황보|제갈|선우|독고|[" + SURNAMES + r"])[가-힣]{1,2}")
# 부서 + 이름('생산관리팀 정민호')은 '마케팅팀 전환율'과 구별되지 않아 약한 근거로만 쓴다 (사실 확인 표에 보여 줄 뿐 막지 않는다)
DEPT_NAME_RE = re.compile(r"(?:팀|본부|센터|그룹|파트)[ \t]+" + NOT_NAMES + _NAME + r"(?=[^가-힣]|$|[이가은는을를의께에과와도랑])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def source_people(files, allow):
    """사내 자료와 리서치 노트에서 사람을 모은다.

    반환: {"names": 막는 근거가 되는 한글 이름, "weak": 부서명 뒤에만 나온 이름, "latin": 영문 이름과 이메일, "ok": 읽은 수, "failed": 못 읽은 파일}
    '님', '씨'만 붙은 표현('지원자님', '정회원님')은 이름으로 모으지 않는다.
    """
    names, weak, latin, ok, failed = set(), set(), set(), 0, []
    for p in files:
        text = read_any(p)
        if text is None:
            failed.append(p.name)
            continue
        ok += 1
        for _, _, name, kind, title in name_candidates(text, allow, with_title=True):
            if kind == "personal":
                names.add(name)
        for m in SOURCE_NAME_RE.finditer(text):
            names.add(C.despace(m.group("name")))
        for m in PEOPLE_LIST_RE.finditer(text):
            for item in re.split(r"[,·/、;]|\s및\s|\s", m.group(1)):
                item = re.sub(r"\(.*?\)|(?:" + TITLES + r")$", "", item.strip(" .·"))
                if LIST_NAME_RE.fullmatch(item) and item not in allow:
                    names.add(item)
        for m in DEPT_NAME_RE.finditer(text):
            weak.add(C.despace(m.group("name")))
        for m in EMAIL_RE.finditer(text):
            latin.add(m.group(0).lower())
            local = [x for x in re.split(r"[._-]", m.group(0).split("@")[0]) if x.isalpha() and len(x) >= 2]
            if len(local) == 2:
                latin.update({f"{local[0]} {local[1]}".lower(), f"{local[1]} {local[0]}".lower()})
    names -= allow
    return {"names": names, "weak": weak - names - allow, "latin": latin, "ok": ok, "failed": failed}


def source_names(files, allow):
    """(막는 근거가 되는 이름 집합, 읽은 파일 수, 못 읽은 파일 이름 목록). 이전 호출을 위한 얇은 포장."""
    s = source_people(files, allow)
    return s["names"], s["ok"], s["failed"]


CATEGORY_RE = re.compile(r"(이름|실명|명칭|수치|숫자|정보|금액|자료|내용|사례|회사명)$")
GENERIC_NOUNS = {"고객사", "고객", "경쟁사", "거래처", "협력사", "협력업체", "계열사", "자회사", "파트너사", "관계사", "외주사", "공급사"}
SEP = r"[\s\-_·./]*"


def loose(term):
    """표기 변형을 같은 말로 보는 패턴.

    영문·숫자 용어는 앞뒤가 낱말 경계여야 하고('SK'는 'task'에 걸리지 않는다) 글자 사이 '-', '_', '.'를 무시한다('Dae-sung').
    세 글자 이상 한글 용어는 글자 사이 띄어쓰기와 구분 기호를 무시한다('대성 모터스', '대성·모터스').
    두 글자 이하 한글 용어('한화', '현대')는 그대로 쓴 경우만, 다른 낱말 안에 든 경우('현대적', '한 화면')는 뺀다.
    """
    t = term.strip()
    if re.fullmatch(r"[\x00-\x7f]+", t):
        tokens = [x for x in re.split(r"[\s\-_·./]+", t) if x]
        inner = lambda tok: r"[\-_·.]?".join(re.escape(c) for c in tok)  # noqa: E731
        return re.compile(r"(?<![A-Za-z0-9])" + SEP.join(inner(x) for x in tokens) + r"(?![A-Za-z0-9])", re.I)
    chars = [c for c in t if not c.isspace()]
    if len(chars) >= 3:
        return re.compile(SEP.join(re.escape(c) for c in chars), re.I)
    return re.compile(r"(?<![가-힣])" + re.escape(t) + r"(?=[^가-힣]|$|[" + NAME_PARTICLES + r"로])")


def allow_set(rules, toc):
    """실명 예외와 필명. '김영호 대표이사'를 적으면 '김영호'라는 이름 자체를 예외로 본다."""
    allow = {C.despace(x) for x in rules["실명 예외"].split(",") if x.strip() and x.strip() != "없음"}
    allow |= {re.sub(r"(?:" + TITLES + r"|대표이사)$", "", x) for x in allow}
    pen = C.basic_info(toc).get("필명", "") if toc else ""
    if pen and "{{" not in pen:
        allow.add(C.despace(pen))
    return {x for x in allow if x}


def leak_policy(toc, rules, files):
    """책 밖으로 나가면 안 되는 것의 기준. verify(원고, 그림 기록, 출처 목록)와 build_docx(완성된 문서)가 같은 기준을 쓴다."""
    mode = rules["실명 금지"].strip()
    on = C.is_yes(mode) or mode == "엄격"
    allow = allow_set(rules, toc)
    nm = re.search(r"^-\s*쓰면 안 되는 것:\s*(.+)$", toc, re.M)
    never = nm.group(1).strip() if nm and "{{" not in nm.group(1) else ""
    items, categories = [], []
    if never and not re.fullmatch(r"(없음|없다|해당\s*없음|-)\.?", never):
        for item in [x.strip(" .'\"") for x in re.split(r"[,;、]", never)]:
            if item and not re.fullmatch(r"(없음|없다|-)", item):
                (categories if CATEGORY_RE.search(item) or item in GENERIC_NOUNS else items).append(item)
    terms = []
    for item in items:
        if 2 <= len(C.despace(item)) <= 40:
            terms.append(item)
            core = re.sub(r"^(?:\S*\d\S*\s+)+", "", item)  # '2027년 인력 재배치 계획' → '인력 재배치 계획'도 본다
            if core != item and len(C.despace(core)) >= 4:
                terms.append(core)
    people = source_people(files, allow) if on else {"names": set(), "weak": set(), "latin": set(), "ok": 0, "failed": []}
    return {"on": on, "strict": mode == "엄격", "allow": allow, "never": never, "never_terms": terms, "categories": categories,
            "forbidden": C.parse_pairs(rules["금지 용어"]), "src_names": people["names"], "src_weak": people["weak"],
            "src_latin": people["latin"], "src_ok": people["ok"], "src_failed": people["failed"]}


def leak_scan(blob, P):
    """blob에서 새면 안 되는 것을 찾는다. [(심각도, 분류, 내용, 위치)]. ℹ️는 사실 확인 표로 넘길 후보다."""
    out = []
    for wrong, right in P["forbidden"]:
        for m in loose(wrong).finditer(blob):
            out.append(("🔴", "금지 용어", f"'{m.group(0)}'" + (f" → '{right}'" if right else ""), m.start()))
    for term in P["never_terms"]:
        for m in loose(term).finditer(blob):
            out.append(("🔴", "쓰면 안 되는 것", f"'{m.group(0)}' (저자 자료의 '쓰면 안 되는 것')", m.start()))
    if not P["on"]:
        return out
    src_sev = "🔴" if P["strict"] else "🟡"
    covered = set()
    for pos, expr, name, kind in name_candidates(blob, P["allow"]):
        covered.add(pos)
        if name in P["src_names"] and kind != "honorific":
            out.append((src_sev, "사내 자료에 나온 실명", f"{expr} (실명이 아니면 검증 규칙 '실명 예외'에 적는다)", pos))
        elif P["strict"]:
            label = {"public": "공인 실명 확인", "honorific": "실명일 수 있는 호칭"}.get(kind, "실명으로 보이는 인물")
            out.append(("🔴" if kind == "personal" else "🟡", label, f"{expr} (실명이 아니면 검증 규칙 '실명 예외'에 적는다)", pos))
        else:
            out.append(("ℹ️", "실명 후보", expr, pos))
    for names, sev, cat in ((P["src_names"], src_sev, "사내 자료에 나온 실명"), (P["src_weak"], "ℹ️", "자료에 부서명과 함께 나온 낱말")):
        for name in sorted(names):
            forms = [re.escape(name)]
            if len(name) == 3:  # '지훈 씨', '민재 상무님'처럼 이름만 쓴 호칭
                forms.append(re.escape(name[1:]) + r"(?=[ \t]?(?:씨|님|" + TITLES + r"))")
            use = re.compile(r"(?<![가-힣])(?:" + "|".join(forms) + r")(?=[^가-힣]|$|[" + NAME_PARTICLES + r"]|[ \t]?(?:" + TITLES + r"))")
            for m in use.finditer(blob):
                if m.start() in covered or m.start() - 1 in covered:
                    continue
                covered.add(m.start())
                out.append((sev, cat, f"{m.group(0)} (자료에 사람으로 나온 이름. 실명이 아니면 '실명 예외'에 적는다)", m.start()))
    for latin in sorted(P["src_latin"]):
        for m in loose(latin).finditer(blob):
            out.append((src_sev, "사내 자료에 나온 영문 이름·이메일", m.group(0), m.start()))
    return out


NOUN_YO = ("필요", "중요", "수요", "주요", "개요", "소요", "강요", "동요", "요요")
# 1인칭 단수 화자 표현. '내용', '내부'처럼 '내'로 시작하는 낱말과 '우리', '저희'(조직의 말)는 제외한다
FIRST_PERSON_RE = re.compile(r"(?<![가-힣])(나는|나도|내가|나의|나에게|나한테|내게|저는|저도|제가|저의|저에게|저한테|제게|필자는|필자가)")
# '제 + 명사'는 1인칭일 수도, 관용구('제 역할', '제 시간에')일 수도 있어 🟡로만 알린다
FIRST_PERSON_SOFT_RE = re.compile(r"(?<![가-힣])(?:제|내)\s(?!역할|성능|시간|기능|몫|때|자리|값|구실|실력|모습|위치|날짜|속도|궤도|가격)(?=[가-힣])")


def figure_texts(toc_path, src, text, add=None):
    """원고에 있는 그림의 기록된 글자. 그림 파일과 해시가 다르거나 기록이 없으면 넣지 않고 ℹ️로 알린다 (사실 확인 표에는 ⚠️로 나온다)."""
    img = toc_path.resolve().parent.parent / "output" / src.resolve().parent.name / "images"
    data = {}
    if (img / "figdata.json").is_file():
        try:
            data = json.loads((img / "figdata.json").read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    out, stale = [], []
    for n in sorted({int(x) for x in FIG_RE.findall(text) for x in [x[0]]}):
        key = f"fig{n:02d}"
        rec = data.get(key)
        png = next((img / f"{key}{e}" for e in (".png", ".jpg") if (img / f"{key}{e}").is_file()), None)
        if rec and png and rec.get("sha") == C.sha(png) and not rec.get("has_image"):
            out.append((key, " / ".join(map(str, rec.get("texts", []) + rec.get("cells", [])))))
        elif png:
            stale.append(key)
            if rec and rec.get("sha") == C.sha(png) and rec.get("has_image"):
                out.append((key, " / ".join(map(str, rec.get("texts", []) + rec.get("cells", [])))))  # 기록된 글자는 검사하고, 이미지 속 글자는 사람이 본다
    if stale and add:
        add("ℹ️", "검사기가 글자를 읽지 못한 그림", f"{', '.join(stale[:8])} (그림 도구 밖에서 만들었거나, 다시 저장됐거나, 캡처 이미지가 들어 있다. "
            "사실 확인 때 그림을 열어 사람이 본다)")
    return out


def citation_texts(cite_arg, src):
    """책 끝 출처 목록에 실제로 찍히는 글. build_docx와 같은 함수(C.citation_line)로 만든다 (claim은 책에 찍히지 않는다)."""
    cpath = pathlib.Path(cite_arg) if cite_arg else src.with_name("01_citations.json")
    try:
        cites = json.loads(cpath.read_text(encoding="utf-8")) if cpath.is_file() else []
        return [(str(c.get("id", "?")), C.citation_line(c)) for c in cites if isinstance(c, dict)]
    except (ValueError, UnicodeDecodeError):
        return []


def build_masks(text):
    """검사 대상에서 뺄 영역을 공백으로 바꾼 사본을 만든다. 줄 번호는 그대로 유지한다.

    prose: 코드 블록, 인라인 코드, HTML 주석을 뺀 본문 (부호·용어 검사용)
    ending: prose에서 헤딩, 인용 블록(>), 표, 따옴표 안 대사까지 뺀 서술문 (어미 검사용)
    """
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    prose = re.sub(r"^(```|~~~).*?^\1[^\n]*$", blank, text, flags=re.M | re.S)
    prose = re.sub(r"`[^`\n]+`", blank, prose)
    prose = re.sub(r"<!--.*?-->", blank, prose, flags=re.S)

    ending_lines = []
    for line in prose.split("\n"):
        s = line.lstrip()
        if s.startswith("#") or s.startswith(">") or s.startswith("|"):
            ending_lines.append(" " * len(line))
        else:
            ending_lines.append(line)
    ending = "\n".join(ending_lines)
    ending = re.sub(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|「[^」\n]*」|(?<![가-힣A-Za-z])\'[^\'\n]*\'', blank, ending)
    ending = ending.replace("**", "").replace("*", "")  # '**…입니다**.', '*…다*.'처럼 강조 닫힘 뒤 마침표도 종결로 읽는다
    ending = re.sub(r"[ \t]*\([^()\n]{0,40}\)(?=[.!?…])", "", ending)  # '좋습니다(2024년 기준).'의 괄호 주석
    # 출처 태그가 마침표 앞에 붙어도 종결을 읽을 수 있게 지운다 ("했다 [[cite_001]]." → "했다.")
    ending = re.sub(r"[ \t]*\[\[[^\]\n]*\]\]", "", ending)  # 줄바꿈은 지우지 않는다 (줄 번호 유지)
    return prose, ending


def norm_num(tok):
    """'1,500 원' → '1500원', '1만 5천 원' → '15000원', '300여 명' → '300명'. 출처 claim과 본문 수치를 대조할 때 쓴다."""
    from decimal import Decimal, InvalidOperation
    s = re.sub(r"[\s,]", "", tok).rstrip(".")

    def plain(x):  # Decimal → '2', '2.5' (끝의 0 없이, 지수 표기 없이)
        t = format(x, "f")
        return t.rstrip("0").rstrip(".") if "." in t else t

    m = re.match(r"^(\d[\d.백천만억조]*?)(?:여)?([^\d.백천만억조].*|[만억조]?)$", s)
    if not m or not re.search(r"[백천만억조]", m.group(1) + m.group(2)[:1]):
        s = re.sub(r"(?<=\d)여", "", s)
        return re.sub(r"\d+\.\d+", lambda x: plain(Decimal(x.group(0))), s)  # '2.0%' = '2%'
    num, unit = m.group(1), m.group(2)
    if unit[:1] in ("만", "억", "조"):
        num, unit = num + unit[0], unit[1:]
    total, group, cur = Decimal(0), Decimal(0), None
    try:
        for t in re.findall(r"\d+(?:\.\d+)?|[백천만억조]", num):
            if t[0].isdigit():
                cur = Decimal(t)
            elif t in ("백", "천"):
                group += (cur if cur is not None else 1) * (100 if t == "백" else 1000)
                cur = None
            else:
                group += cur or 0
                total += (group or 1) * {"만": 10 ** 4, "억": 10 ** 8, "조": 10 ** 12}[t]
                group, cur = Decimal(0), None
    except InvalidOperation:
        return s
    return plain(total + group + (cur or 0)) + unit


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="원고 자동 검사기")
    ap.add_argument("draft")
    ap.add_argument("legacy_toc", nargs="?", help="(구 사용법 호환) toc 경로")
    ap.add_argument("legacy_report", nargs="?", help="(구 사용법 호환) 보고서 경로")
    ap.add_argument("--toc")
    ap.add_argument("--report")
    ap.add_argument("--citations")
    ap.add_argument("--min-score", type=int, default=9)
    args = ap.parse_args(argv)

    src = pathlib.Path(args.draft)
    if not src.is_file():
        print(f"원고 파일이 없습니다: {src}", file=sys.stderr)
        return 2
    toc_path = pathlib.Path(args.toc or args.legacy_toc) if (args.toc or args.legacy_toc) else C.find_toc()
    if not toc_path or not toc_path.is_file():
        print("user_input/user-book-toc.md를 찾지 못했습니다. --toc로 지정하세요.", file=sys.stderr)
        return 2
    report_path = pathlib.Path(args.report or args.legacy_report or src.with_name("verify-report.md"))

    try:
        import unicodedata
        text = unicodedata.normalize("NFC", src.read_text(encoding="utf-8"))
        toc = toc_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        print(f"UTF-8로 읽을 수 없는 파일입니다 ({e.reason}). 원고와 책 설정은 UTF-8로 저장하세요.", file=sys.stderr)
        return 2
    issues = []  # (심각도, 분류, 상세, 줄 번호 또는 None)

    def add(sev, cat, detail, line=None):
        issues.append((sev, cat, re.sub(r"\s+", " ", str(detail)).strip(), line))

    # ------------------------------------------------------------------ 규칙
    rules_sec = C.get_section(toc, "검증 규칙")
    values, unknown = C.parse_kv(rules_sec, RULE_DEFAULTS.keys())
    rules = dict(RULE_DEFAULTS)
    rules.update({k: v for k, v in values.items() if v})
    rules = {k: ("" if v.strip() in ("없음", "-") else v) for k, v in rules.items()}
    if rules_sec is None:
        add("ℹ️", "검증 규칙 섹션 없음", "템플릿 기본값으로 검사함")
    for k in unknown:
        add("🟡", "알 수 없는 규칙 키", f"'{k}' (오타라면 기본값으로 검사되고 있음)")

    prose, ending_text = build_masks(text)

    # 1) em dash: Word 결과물에 실리는 모든 글(코드 블록 포함)을 본다. build_docx 사후 검사와 같은 기준이다.
    if not C.is_yes(rules["em dash 허용"]):
        for m in re.finditer(r"[—–]", re.sub(r"<!--.*?-->", lambda x: re.sub(r"[^\n]", " ", x.group(0)), text, flags=re.S)):
            add("🔴", "em dash 잔존", f"'{m.group(0)}'", line_of(text, m.start()))

    # 2) 강조 기호: 변환기와 같은 규칙(C.INLINE_SPLIT_RE)으로 나눠 보고, 변환 뒤에 남을 ** 와 굵은 글씨 안의 출처 태그를 잡는다
    for i, line in enumerate(prose.split("\n"), 1):
        if "*" not in line and "__" not in line:
            continue
        parts = C.INLINE_SPLIT_RE.split(line)
        if "**" in "".join(parts[::2]):
            add("🔴", "짝이 맞지 않는 **", line.strip()[:40], i)
        if any(t.startswith(("**", "__")) and "[[" in t for t in parts[1::2]):
            add("🔴", "굵은 글씨 안의 출처 태그", "출처 태그는 굵은 글씨 밖에 둔다 (변환되지 않고 그대로 찍힌다)", i)

    # 3) 허용되지 않은 HTML 주석, 중간 단계 마커 잔존
    for m in re.finditer(r"<!--.*?-->", text, re.S):
        if not ALLOWED_COMMENTS.fullmatch(m.group(0)):
            add("🟡", "알 수 없는 주석", m.group(0)[:40], line_of(text, m.start()))
    markers = list(C.MARKER_RE.finditer(text))
    stem = src.stem
    expect_marker = bool(re.match(r"^\d\d_", stem))
    for m in markers:
        if m.group(1) != stem:
            add("🟡", "중간 단계 마커 잔존", m.group(0), line_of(text, m.start()))
    if expect_marker:
        last = text.rstrip().splitlines()[-1].strip() if text.strip() else ""
        if not any(m.group(1) == stem for m in markers):
            add("🔴", "완료 마커 없음", f"<!-- STAGE_COMPLETE: {stem} -->")
        elif last != f"<!-- STAGE_COMPLETE: {stem} -->":
            add("🟡", "완료 마커 위치", "마지막 줄이 아님")

    # 4) 수평선 (장 구분은 헤딩으로만 한다)
    for i, line in enumerate(prose.split("\n"), 1):
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            add("🟡", "수평선 사용", "장 구분은 헤딩으로만 한다", i)

    # 5) 어미 통일성
    ending = rules["어미"].strip()
    END = r"[)\]」』'\"]*(?:[.!?]|…)"
    hap = [m.start() for m in re.finditer(r"((?<![아다])니다|니까|십시오)" + END, ending_text)]  # '아니다.', '다니다.'는 해라체
    # '필요.', '중요.' 같은 명사 종결은 해요체가 아니다
    haeyo = [m.start() for m in re.finditer(r"([가-힣])요" + END, ending_text) if m.group(1) + "요" not in NOUN_YO]
    haeyo += [m.start() for m in re.finditer(r"[가-힣]죠" + END, ending_text)]
    hae = [m.start() for m in re.finditer(r"(?:(?<!니)|(?<=[아다]니))다" + END, ending_text)]
    total = len(hap) + len(haeyo) + len(hae)
    groups = {"해라체": hae, "합쇼체": hap, "해요체": haeyo}
    if ending in groups and total:
        intrusions = sorted(p for k, v in groups.items() if k != ending for p in v)
        ratio = len(intrusions) / total
        if ratio > 0.05:
            where = ", ".join(str(line_of(ending_text, p)) for p in intrusions[:5])
            add("🟡", "어미 혼용", f"{ending} 외 종결 {len(intrusions)}/{total} ({ratio:.0%}), 예: {where}행")
    elif ending and ending not in groups:
        add("🟡", "알 수 없는 어미 값", f"'{ending}' (해라체, 합쇼체, 해요체 중 하나)")
    elif not ending:
        add("ℹ️", "어미 검사", "규칙 없음, 생략")

    # 6) 분량
    char_count = len(re.sub(r"\s", "", C.strip_markers(text)))
    rng = C.target_chars(toc)
    if rng:
        lo, hi = rng
        if char_count < lo:
            add("🔴", "분량 미달", f"{char_count:,}자 < {lo:,}자 (목표 {lo:,}~{hi:,})")
        elif char_count > hi * 1.15:
            add("🟡", "분량 초과", f"{char_count:,}자 > {hi:,}자의 115%")
        else:
            add("ℹ️", "분량", f"{char_count:,}자 (목표 {lo:,}~{hi:,})")
    else:
        add("ℹ️", "분량", f"{char_count:,}자 (기본 정보에 'N~M자' 목표 없음)")

    # 6-1) 예상 쪽수 (LibreOffice가 없어도 초안 단계에서 분량을 가늠한다. 실측은 build_docx --pdf)
    # 쪽당 글자 수 = 780 - 80 × (1만 자당 그림 수). 실측 두 점에 맞춘 식이다:
    # 그림 많은 원고(133,481자, 그림 41개) 232~262쪽, 글 위주 원고(83,363자, 그림 4개) 111쪽 (B5, 11pt)
    figs_n = len(FIG_RE.findall(prose))
    per_page = max(450, 780 - 80 * (figs_n / max(1, char_count / 10000)))
    pw, ph = C.paper_mm(C.parse_kv(C.get_section(toc, "출판 설정"), ["판형"])[0].get("판형"))
    per_page *= (pw * ph) / (176 * 250)  # 식은 B5 실측이다. 판형 넓이에 비례해 쪽당 글자 수를 늘리거나 줄인다 (A4 80쪽 실측과 맞춤)
    est = char_count / per_page
    est_lo, est_hi = round(est * 0.9), round(est * 1.1)
    pm = re.search(r"(\d+)\s*(?:~\s*(\d+)\s*)?(?:페이지|쪽)", C.basic_info(toc).get("목표 분량", ""))
    if pm and char_count > 2000:
        t_lo, t_hi = int(pm.group(1)), int(pm.group(2) or pm.group(1))
        if est_hi < t_lo * 0.9 or est_lo > t_hi * 1.1:
            add("🟡", "예상 쪽수와 목표 차이", f"예상 {est_lo}~{est_hi}쪽(쪽당 약 {round(per_page)}자), 목표 {pm.group(0)}. 글자 수 목표를 조정하거나 build_docx --pdf로 실측한다")
        else:
            add("ℹ️", "예상 쪽수", f"{est_lo}~{est_hi}쪽 (이 원고의 그림 밀도로 쪽당 약 {round(per_page)}자)")

    # 7) 목차 일치: 부(# 헤딩)와 장(## 이하 헤딩)을 라벨로 대조한다
    structure = C.toc_structure(toc)
    if not structure:
        add("🔴", "목차 구조를 읽지 못함", "toc의 '## 목차' 아래 '### 부 제목'이 있어야 한다")
    heads = C.iter_headings(text)
    h1 = [C.despace(C.split_label(t)[0]) for _, lv, t in heads if lv == 1]
    h2 = [C.despace(C.split_label(t)[0]) for _, lv, t in heads if lv >= 2]
    def norm_title(s):
        return re.sub(r"[\s:.,·'\"“”‘’]", "", re.sub(r"<br\s*/?>", "", s))

    h1_full = {C.despace(C.split_label(t)[0]): norm_title(t) for _, lv, t in heads if lv == 1}
    for div_title, chapters in structure:
        label, _ = C.split_label(div_title)
        key = C.despace(label)
        if key not in h1:
            add("🔴", "목차 항목 누락", f"부 '{div_title}' (원고에 '# {label}…' 헤딩 없음)")
        elif norm_title(div_title) != h1_full.get(key, ""):
            add("🟡", "부 제목 불일치", f"목차 '{div_title}'")
        for ch in chapters:
            clabel, _ = C.split_label(ch)
            if C.despace(clabel) not in h2:
                add("🔴", "목차 항목 누락", f"장 '{ch[:40]}'")
    # 목차에 없는 부 헤딩 (예: 원고 맨 앞의 책 제목 '# …'은 표지와 겹친다)
    toc_labels = {C.despace(C.split_label(d)[0]) for d, _ in structure}
    for ln, lv, t in heads:
        if lv == 1 and C.despace(C.split_label(t)[0]) not in toc_labels:
            add("🔴", "목차에 없는 부 헤딩", f"'# {t[:40]}' (책 제목은 원고에 넣지 않는다)", ln + 1)

    # 조직 화자는 1인칭 체험을 쓰지 않는다 (인용문, 인용 블록, 코드는 제외)
    author_sec = C.get_section(toc, "저자 자료") or ""
    vm = re.search(r"화자 성격:\s*([^\n#]+)", author_sec)
    voice = vm.group(1).strip() if vm else ""
    if voice == "조직 화자":
        for m in FIRST_PERSON_RE.finditer(ending_text):
            add("🔴", "조직 화자의 1인칭 표현", m.group(0).strip(), line_of(ending_text, m.start()))
        for m in FIRST_PERSON_SOFT_RE.finditer(ending_text):
            add("🟡", "조직 화자의 1인칭일 수 있는 표현", ending_text[m.start():m.start() + 8].strip(), line_of(ending_text, m.start()))

    # 실명과 쓰면 안 되는 것: 원문 전체(코드 블록과 프롬프트 예시 포함), 그림 속 글자, 책 끝 출처 목록을 같은 검사기로 본다
    raw = re.sub(r"<!--.*?-->", lambda x: re.sub(r"[^\n]", " ", x.group(0)), text, flags=re.S)
    blobs = [(raw, None)]
    for key, fig_text in figure_texts(toc_path, src, text, add):
        blobs.append((fig_text, f"그림 {key}"))
    for cid, ctext in citation_texts(args.citations, src):
        blobs.append((ctext, f"출처 {cid}"))
    sdir = toc_path.resolve().parent / "sources"
    files = [x for x in sdir.rglob("*") if x.is_file() and not x.name.startswith(".")] if sdir.is_dir() else []
    files += [x for x in [src.resolve().parent / "01_research-notes.md"] if x.is_file()]
    P = leak_policy(toc, rules, files)
    soft = []
    for blob, label in blobs:
        for sev, cat, detail, pos in leak_scan(blob, P):
            ln = None if label else line_of(text, pos)
            pre = f"{label}: " if label else ""
            if sev == "ℹ️":
                soft.append(f"{pre}{detail}" + (f"({ln}행)" if ln else ""))
            else:
                add(sev, cat, pre + detail, ln)
    if soft:
        add("ℹ️", "실명일 수 있는 표현", f"{len(soft)}건: {', '.join(soft[:8])}{' …' if len(soft) > 8 else ''} "
            "(출판 전 사실 확인 표에서 사람이 본다. 사내 배포 책은 '실명 금지: 엄격'으로 막는다)")
    if P["strict"] and P["src_failed"]:
        add("🟡", "읽지 못한 사내 자료", f"{', '.join(P['src_failed'][:5])}: 이 자료의 사람 이름은 검사기가 대조하지 못한다. "
            "PDF나 Word로 저장해 다시 넣거나, 사실 확인 때 사람이 본다")
    if P["on"] and files:
        add("ℹ️", "사내 자료 이름 대조", f"자료 {P['src_ok']}개에서 이름 {len(P['src_names'])}개"
            + (f"({', '.join(sorted(P['src_names'])[:10])})" if P["src_names"] else "")
            + (f", 읽지 못한 자료 {len(P['src_failed'])}개: {', '.join(P['src_failed'][:5])} (PDF는 pdftotext가 있어야 읽는다. "
               "못 읽은 자료의 이름은 사실 확인 표에서 사람이 본다)" if P["src_failed"] else ""))
    if P["categories"] and not rules["금지 용어"]:
        add("🔴", "금지 용어가 비어 있음", f"'쓰면 안 되는 것'의 범주({', '.join(P['categories'])[:40]})는 검사기가 알아볼 수 없다. "
            "실제 회사명, 고객사, 프로젝트명(영문명과 약칭 포함)을 검증 규칙 '금지 용어'에 적는다")

    # 8) 부 브릿지 문단
    bridge_tmpl = rules["부 브릿지 문구"].strip()
    if bridge_tmpl and bridge_tmpl not in ("없음", "-"):
        # 목차에 브릿지가 적힌 부만 요구한다. 목차에 하나도 없으면 모든 부에 요구한다.
        toc_body = C.get_section(toc, "목차") or ""
        bridge_pat = re.escape(bridge_tmpl).replace("N", r"(?:제\s*)?(\d+)")
        part_nums = sorted({int(n) for n in re.findall(bridge_pat, toc_body)})
        if not part_nums:
            part_nums = sorted({int(n) for d, _ in structure for n in re.findall(r"제\s*(\d+)\s*부", d)})
        nospace = C.despace(text)
        for n in part_nums:
            variants = {bridge_tmpl.replace("N", str(n)), bridge_tmpl.replace("N", f"제{n}")}
            if not any(C.despace(v) in nospace for v in variants):
                add("🟡", "브릿지 문단 누락", bridge_tmpl.replace("N", str(n)))

    # 9) 금지 용어는 위의 유출 검사에서 본다. 금지 영어 표현 (🟡)
    for wrong, right in C.parse_pairs(rules["금지 영어 표현"]):
        for m in re.finditer(re.escape(wrong), prose):
            add("🟡", "금지 영어 표현", f"'{wrong}'" + (f" → '{right}'" if right else ""), line_of(text, m.start()))

    # 10) 불릿 부호: 마크다운 목록(-, *)은 변환 때 지정 기호로 바뀌므로 정상이다.
    #     본문에 직접 친 타이포 불릿이 지정 기호와 다르면 🟡.
    bullet = (rules["불릿 기호"].strip() or "•")[0]
    for sym in ["●", "○", "▪", "▫", "■", "□", "◆", "◇", "‣", "⁃", "•"]:
        if sym == bullet:
            continue
        for m in re.finditer(r"^\s*" + re.escape(sym) + r"\s", prose, re.M):
            add("🟡", "불릿 부호 비통일", f"'{sym}' (지정 기호 '{bullet}')", line_of(text, m.start()))

    # 11) 그림 번호 연속성과 장별 그림
    figs = [(int(m.group(1)), m.start()) for m in FIG_RE.finditer(prose)]
    nums = [n for n, _ in figs]
    add("ℹ️", "그림 수", str(len(nums)))
    seen = set()
    for n, pos in figs:
        if n in seen:
            add("🟡", "그림 번호 중복", f"그림 {n}", line_of(text, pos))
        seen.add(n)
    if nums and sorted(seen) != list(range(1, max(seen) + 1)):
        missing = sorted(set(range(1, max(seen) + 1)) - seen)
        add("🟡", "그림 번호 빠짐", ", ".join(map(str, missing[:10])))
    if C.is_yes(rules["장마다 그림 필수"]):
        # '그림 제외 부'(기본: 부록, 참고문헌) 아래의 장은 그림을 요구하지 않는다
        exempt = tuple(C.despace(x) for x in rules["그림 제외 부"].replace("、", ",").split(",") if x.strip())
        h2_lines, cur_div = [], ""
        for ln, lv, t in heads:
            if lv == 1:
                cur_div = C.despace(C.split_label(t)[0])
            elif lv == 2 and not (exempt and cur_div.startswith(exempt)):
                h2_lines.append((ln, t))
            elif lv == 2:
                h2_lines.append((ln, None))
        bridge_key = C.despace(bridge_tmpl.replace("N", "")) if bridge_tmpl else None
        for idx, (ln, t) in enumerate(h2_lines):
            if t is None:
                continue
            if bridge_key and bridge_key in C.despace(re.sub(r"\d", "", t)):
                continue
            nxt = h2_lines[idx + 1][0] if idx + 1 < len(h2_lines) else len(text.splitlines())
            block = "\n".join(prose.split("\n")[ln:nxt])
            if not FIG_RE.search(block):
                add("🟡", "장에 그림 없음", t[:40], ln + 1)

    # 12) 출처 태그
    cite_path = pathlib.Path(args.citations) if args.citations else src.with_name("01_citations.json")
    cites = {}
    if cite_path.is_file():
        try:
            for c in json.loads(cite_path.read_text(encoding="utf-8")):
                cites[c["id"]] = c
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
            add("🔴", "출처 파일 형식 오류", f"{cite_path.name}: {e}")
    used = set()
    for m in CITE_RE.finditer(prose):
        ids = [x.strip() for x in m.group(1).split(",")]
        used.update(ids)
        if cites:
            unknown_ids = [x for x in ids if x not in cites]
            for x in unknown_ids:
                add("🔴", "없는 출처 ID", x, line_of(text, m.start()))
            tiers = [cites[x].get("tier") for x in ids if x in cites]
            if tiers and all(t == 3 for t in tiers):
                add("🟡", "블로그·SNS 출처만 인용", ", ".join(ids) + " (공식 자료나 언론·연구 출처를 함께 붙인다)", line_of(text, m.start()))
    if used and not cites:
        add("🔴", "출처 파일 없음", f"[[cite_…]] 태그 {len(used)}종이 있지만 {cite_path.name}이 없음")
    for cid in sorted(used & set(cites)):
        c = cites[cid]
        if not str(c.get("title", "")).strip() or not (c.get("url") or c.get("path")):
            add("🔴", "출처 정보 부족", f"{cid}: title과 url(또는 path)이 있어야 책 끝 출처 목록에 제대로 찍힌다")
    # Tier 0(사내·저자 자료)은 파일이 실제로 있어야 한다
    for cid, c in cites.items():
        if c.get("path"):
            cp = str(c["path"])
            cands = [pathlib.Path.cwd() / cp] + ([cite_path.resolve().parents[2] / cp] if len(cite_path.resolve().parents) > 2 else [])
            if not any(x.is_file() for x in cands):
                add("🔴", "출처 파일 경로 없음", f"{cid}: {c['path']}")
    level = rules["수치 출처 필수"].strip()
    examples = 0
    if C.is_yes(level) or level == "엄격":
        # 문장 단위로 본다 (표는 행 단위). 같은 문장에 출처 태그나 [[예시]] 표시가 있어야 한다.
        # 같은 수치(예: '팀 50명')가 여러 번 나오면 한 건으로 묶는다. 한 사실이 점수를 독차지하지 않게 한다.
        sev = "🔴" if level == "엄격" else "🟡"
        groups = {}
        for ln_no, line in enumerate(prose.split("\n"), 1):
            if line.lstrip().startswith(("#", ">")):  # 헤딩과 인용 블록(프롬프트, 대사)은 제외
                continue
            for sent in re.split(r"(?<=[.!?。])\s+", line):
                found = [x for x in NUMERIC_CLAIM_RE.finditer(sent)]
                if RHETORIC_RE.search(sent):
                    found = [x for x in found if norm_num(x.group(0)) not in RHETORIC_TOKENS]
                if not found:
                    continue
                if EXAMPLE_TAG in sent:
                    examples += 1
                elif not CITE_RE.search(sent):
                    # 같은 사실은 한 묶음: 수치와 바로 앞 낱말이 같으면 같은 사실로 본다 ('팀 50명'은 한 번, '설문 34%'와 'SaaS 34%'는 따로)
                    for x in found:
                        if norm_num(x.group(0)) in RHETORIC_TOKENS:
                            continue
                        prev = "".join(re.findall(r"[가-힣A-Za-z]+", sent[:x.start()])[-1:])
                        groups.setdefault((prev, C.despace(x.group(0))), []).append((ln_no, sent.strip()))
                elif cites:
                    # 출처 기록(claim)과 대조: 수치와 단위가 claim에 그대로 있어야 한다 ('63%' 기록으로 '63명'을 뒷받침할 수 없다)
                    ids = [x.strip() for c in CITE_RE.findall(sent) for x in c.split(",")]
                    claims = " ".join(str(cites[i].get("claim", "")) for i in ids if i in cites)
                    have = {norm_num(x.group(0)) for x in NUMERIC_CLAIM_RE.finditer(claims)}
                    missing = sorted({norm_num(x.group(0)) for x in found} - have)
                    if claims.strip() and missing:
                        add(sev, "출처 기록에 없는 수치", f"{', '.join(missing)} ({', '.join(ids)}의 claim에 없음)", ln_no)
        by_line = {}
        for (prev, token), hits in groups.items():
            ln_no, sent = hits[0]
            others = sorted({h[0] for h in hits[1:]} - {ln_no})
            more = f", 같은 수치 {len(others) + 1}곳: {', '.join(map(str, [ln_no] + others[:5]))}행" if others else ""
            entry = by_line.setdefault(ln_no, {"tokens": [], "sent": sent})
            entry["tokens"].append(token + more)
        for ln_no, entry in by_line.items():
            add(sev, "출처 없는 수치", f"{'; '.join(entry['tokens'])} | {entry['sent'][:40]}", ln_no)
    if examples:
        add("ℹ️", "예시 표시 수치", f"{examples}건 ([[예시]]는 출판 전 사실 확인 표에 따로 나온다)")
    if cites:
        add("ℹ️", "출처 사용", f"{len(used & set(cites))}/{len(cites)}개 인용됨")

    # ------------------------------------------------------------------ 점수
    red = sum(1 for s, *_ in issues if s == "🔴")
    yellow = sum(1 for s, *_ in issues if s == "🟡")
    info = sum(1 for s, *_ in issues if s == "ℹ️")
    score = anchor_score(red, yellow)

    if red:
        verdict = "실패 (🔴 있음)"
        code = 1
    elif score < args.min_score:
        verdict = f"보류 (점수 {score} < 기준 {args.min_score})"
        code = 3
    else:
        verdict = "통과"
        code = 0

    lines = [
        "# verify-report",
        "",
        f"- 원고: `{src.name}`",
        f"- 원고 해시: {C.sha(src)}",
        f"- 책 설정 해시: {C.sha(toc)}",
        f"- 책 설정: `{toc_path}`",
        f"- 🔴 필수: {red}건",
        f"- 🟡 권장: {yellow}건",
        f"- ℹ️ 정보: {info}건",
        f"- 규칙 준수 점수: {score} / 10",
        f"- 판정: {verdict}",
        "",
        "이 점수는 서식과 규칙 준수만 잽니다. 내용의 좋고 나쁨은 리뷰어 9인이 봅니다. 해시는 기록용입니다.",
        "",
        "## 상세",
    ]
    order = {"🔴": 0, "🟡": 1, "ℹ️": 2}
    for s, cat, detail, ln in sorted(issues, key=lambda x: (order[x[0]], x[3] or 0)):
        where = f" ({ln}행)" if ln else ""
        lines.append(f"- {s} {cat}{where}: {detail}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"verify: 🔴 {red}, 🟡 {yellow}, ℹ️ {info}, 점수 {score}/10, {verdict} → {report_path}")
    return code


def anchor_score(red, yellow):
    """SKILL.md '점수 기준' 표. 🔴과 🟡 각각의 상한 중 낮은 쪽."""
    if red == 0:
        red_cap = 10
    elif red == 1:
        red_cap = 7
    elif red == 2:
        red_cap = 6
    else:
        red_cap = max(1, 5 - (red - 3))
    if yellow == 0:
        y_cap = 10
    elif yellow <= 2:
        y_cap = 9
    elif yellow <= 5:
        y_cap = 8
    elif yellow <= 10:
        y_cap = 7
    elif yellow <= 15:
        y_cap = 6
    else:
        y_cap = 5
    return min(red_cap, y_cap)


if __name__ == "__main__":
    sys.exit(main())
