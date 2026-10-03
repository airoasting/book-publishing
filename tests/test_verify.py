"""verify.py 회귀 테스트. 실행: python3 -m unittest discover -s tests

각 테스트는 실제로 있었던 결함 하나를 막는다. 테스트 이름 옆 주석이 그 결함이다.
"""
import contextlib
import io
import pathlib
import re
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import verify  # noqa: E402

FIX = HERE / "fixtures"
GOOD = (FIX / "15_manuscript.md").read_text(encoding="utf-8")
TOC = (FIX / "toc.md").read_text(encoding="utf-8")
STRICT = TOC.replace("- 장마다 그림 필수: 예", "- 장마다 그림 필수: 예\n- 실명 금지: 엄격")
assert STRICT != TOC


def run(manuscript, toc=TOC, name="15_manuscript.md", extra=()):
    """원고와 toc를 임시 폴더에 쓰고 verify를 돌려 (종료 코드, 보고서)를 돌려준다."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / name).write_text(manuscript, encoding="utf-8")
        (d / "toc.md").write_text(toc, encoding="utf-8")
        (d / "01_citations.json").write_text((FIX / "01_citations.json").read_text(encoding="utf-8"), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = verify.main([str(d / name), "--toc", str(d / "toc.md"), "--report", str(d / "r.md"), *extra])
        return code, (d / "r.md").read_text(encoding="utf-8")


def count(report, sev, cat=None):
    lines = [l for l in report.splitlines() if l.startswith(f"- {sev}")]
    if cat:
        lines = [l for l in lines if cat in l]
    return len(lines)


def run_in_work(manuscript, toc=TOC, sources=None, figdata=None):
    """작업 폴더 구조(user_input/, draft/{책}/, output/{책}/images/)를 만들어 verify를 돌린다. 사내 자료와 그림 기록을 함께 본다."""
    import json
    with tempfile.TemporaryDirectory() as w:
        w = pathlib.Path(w)
        (w / "user_input" / "sources").mkdir(parents=True)
        (w / "draft" / "20261003").mkdir(parents=True)
        (w / "output" / "20261003" / "images").mkdir(parents=True)
        (w / "user_input" / "user-book-toc.md").write_text(toc, encoding="utf-8")
        for name, body in (sources or {}).items():
            (w / "user_input" / "sources" / name).write_text(body, encoding="utf-8")
        if figdata is not None:
            img = w / "output" / "20261003" / "images"
            for key, rec in figdata.items():
                (img / f"{key}.png").write_bytes(key.encode())
                rec.setdefault("sha", verify.C.sha(img / f"{key}.png"))
            (img / "figdata.json").write_text(json.dumps(figdata, ensure_ascii=False), encoding="utf-8")
        ms = w / "draft" / "20261003" / "15_manuscript.md"
        ms.write_text(manuscript, encoding="utf-8")
        (ms.parent / "01_citations.json").write_text((FIX / "01_citations.json").read_text(encoding="utf-8"), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = verify.main([str(ms), "--toc", str(w / "user_input" / "user-book-toc.md"), "--report", str(w / "r.md")])
        return code, (w / "r.md").read_text(encoding="utf-8")


class TestVerify(unittest.TestCase):
    def test_good_manuscript_scores_10(self):
        code, rep = run(GOOD)
        self.assertEqual(code, 0, rep)
        self.assertIn("규칙 준수 점수: 10 / 10", rep)

    def test_toc_check_detects_missing_chapter(self):
        # 결함: '## 목차' 바로 아래 '### 부' 헤딩에서 섹션이 끊겨 목차 검사가 꺼져 있었다 (커밋 230bb08)
        bad = GOOD.replace("## 제2장. 두 번째 장", "## 엉뚱한 제목")
        code, rep = run(bad)
        self.assertEqual(code, 1)
        self.assertEqual(count(rep, "🔴", "목차 항목 누락"), 1, rep)
        self.assertIn("제2장", rep)

    def test_empty_manuscript_fails(self):
        # 결함: 43자짜리 빈 원고가 목차 누락 0건, 7점을 받았다
        code, rep = run("짧다.\n\n<!-- STAGE_COMPLETE: 15_manuscript -->\n")
        self.assertEqual(code, 1)
        self.assertEqual(count(rep, "🔴", "목차 항목 누락"), 8)  # 부 4 + 장 4
        self.assertEqual(count(rep, "🔴", "분량 미달"), 1)

    def test_sublist_items_are_not_chapters(self):
        # 목차의 들여쓴 하위 목록과 작성 지시는 장으로 세지 않는다
        _, rep = run(GOOD)
        self.assertNotIn("하위 항목", rep)
        self.assertNotIn("이유를 말한다", rep)

    def test_balanced_bold_is_allowed_in_markdown(self):
        # 결함: 마크다운 원고의 **강조** 330건이 전부 🔴이 되어 실제 원고가 1점을 받았다
        _, rep = run(GOOD)
        self.assertEqual(count(rep, "🔴", "**"), 0)

    def test_orphan_bold_marker_is_red(self):
        code, rep = run(GOOD.replace("**강조**는", "**강조는"))
        self.assertEqual(code, 1)
        self.assertEqual(count(rep, "🔴", "짝이 맞지 않는"), 1)

    def test_em_dash_is_red_everywhere(self):
        code, rep = run(GOOD.replace("첫 장이다.", "첫 장이다 — 정말이다."))
        self.assertEqual(count(rep, "🔴", "em dash"), 1)
        code, rep = run(GOOD.replace("**별표**도", "줄표 — 도"))
        self.assertEqual(count(rep, "🔴", "em dash"), 1, "코드 블록도 Word에 실리므로 검사한다")

    def test_em_dash_allowed_when_rule_says_so(self):
        toc = TOC.replace("em dash 허용: 아니오", "em dash 허용: 예")
        _, rep = run(GOOD.replace("첫 장이다.", "첫 장이다 — 정말이다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "em dash"), 0)

    def test_quotes_code_and_blockquotes_are_excluded_from_ending_check(self):
        # 결함: 인용문과 프롬프트 예시 속 합쇼체까지 세어 어미 혼용을 잘못 잡았다
        _, rep = run(GOOD)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 0)

    def test_hapsyo_intrusion_detected(self):
        bad = GOOD.replace("첫 장이다.", "첫 장입니다. 정말 그렇습니다. 맞습니다.")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)

    def test_haeyo_intrusion_detected(self):
        # 결함: '다.'만 세서 해요체(~요.) 혼입을 놓쳤다
        bad = GOOD.replace("실습이다.", "실습이에요. 따라 해 보세요. 쉬워요.")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)

    def test_unknown_rule_key_is_reported(self):
        # 결함: 규칙 키 오타가 경고 없이 기본값으로 돌았다
        toc = TOC.replace("- em dash 허용: 아니오", "- em-dash 허용: 아니오")
        _, rep = run(GOOD, toc=toc)
        self.assertEqual(count(rep, "🟡", "알 수 없는 규칙 키"), 1)

    def test_explanation_lines_are_not_rule_keys(self):
        _, rep = run(GOOD)
        self.assertEqual(count(rep, "🟡", "알 수 없는 규칙 키"), 0)

    def test_banned_term(self):
        code, rep = run(GOOD.replace("첫 장이다.", "커스텀 지시를 쓴다."))
        self.assertEqual(count(rep, "🔴", "금지 용어"), 1)

    def test_unknown_citation_is_red(self):
        code, rep = run(GOOD.replace("[[cite_001]]", "[[cite_999]]"))
        self.assertEqual(count(rep, "🔴", "없는 출처 ID"), 1)

    def test_tier3_only_citation_is_yellow(self):
        _, rep = run(GOOD.replace("[[cite_001]]", "[[cite_002]]"))
        self.assertEqual(count(rep, "🟡", "블로그·SNS 출처만"), 1)

    def test_numeric_claim_without_citation(self):
        _, rep = run(GOOD.replace(" [[cite_001]]", ""))
        self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 1)

    def test_chapter_without_figure(self):
        _, rep = run(GOOD.replace("[그림 4: 실습 그림]", "").replace("[그림 5: 체크리스트]", "[그림 4: 체크리스트]"))
        self.assertEqual(count(rep, "🟡", "장에 그림 없음"), 1)

    def test_figure_numbering(self):
        _, rep = run(GOOD.replace("[그림 3: 둘째 장 그림]", "[그림 2: 둘째 장 그림]"))
        self.assertEqual(count(rep, "🟡", "그림 번호 중복"), 1)

    def test_bridge_required_only_where_toc_lists_it(self):
        _, rep = run(GOOD)
        self.assertEqual(count(rep, "🟡", "브릿지"), 0)
        _, rep = run(GOOD.replace("## 1부를 마치며", "## 마무리"))
        self.assertEqual(count(rep, "🟡", "브릿지"), 1)

    def test_marker_rules(self):
        code, rep = run(GOOD.replace("<!-- STAGE_COMPLETE: 15_manuscript -->", ""))
        self.assertEqual(count(rep, "🔴", "완료 마커 없음"), 1)
        _, rep = run(GOOD.replace("# 제2부", "<!-- STAGE_COMPLETE: 15_manuscript_p03 -->\n\n# 제2부"))
        self.assertEqual(count(rep, "🟡", "중간 단계 마커"), 1)

    def test_exit_code_3_below_min_score(self):
        code, rep = run(GOOD.replace("첫 장이다.", "Before/After를 본다."), extra=["--min-score", "10"])
        self.assertEqual(code, 3, rep)

    def test_report_has_line_numbers(self):
        _, rep = run(GOOD.replace("첫 장이다.", "커스텀 지시를 쓴다."))
        self.assertTrue(re.search(r"금지 용어 \(\d+행\)", rep), rep)

    def test_neutral_defaults_without_rules_section(self):
        # 결함: 규칙 섹션을 지운 다른 책이 특정 책(NotebookLM)의 금지 용어를 물려받았다
        toc = re.sub(r"## 검증 규칙.*?(?=## 출판 설정)", "", TOC, flags=re.S)
        _, rep = run(GOOD.replace("첫 장이다.", "커스텀 지시를 쓴다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "금지 용어"), 0)
        self.assertIn("검증 규칙 섹션 없음", rep)

    # ---- 2차 평가에서 나온 오탐·미탐 ----
    def test_noun_endings_are_not_haeyo(self):
        # 결함: '중요.', '필요.' 같은 명사 종결을 해요체로 세어 83% 혼용으로 잡았다
        bad = GOOD.replace("실습이다.", "실습이다. 순서가 중요. 확인이 필요. 반복이 중요. 기록도 필요.")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 0, rep)

    def test_hapsyo_imperative_and_cite_before_period(self):
        # 결함: '여십시오.'를 놓쳤고, '했다 [[cite_001]].'은 어미 검사를 피했다
        bad = GOOD.replace("실습이다.", "노트북을 여십시오. 지금 여십시오. 바로 여십시오.")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)
        bad = GOOD.replace("첫 장이다. 시장 점유율은 42%다 [[cite_001]].", "첫 장입니다 [[cite_001]]. 점유율은 42%입니다 [[cite_001]]. 맞습니다 [[cite_001]].")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)

    def test_numeric_units(self):
        # 결함: '3천만 원', '2배로', '300명', '30유로'를 수치로 보지 않았다
        for phrase in ("예산은 3천만 원이다.", "속도가 2배로 늘었다.", "직원 300명이 썼다.", "가격은 30유로다."):
            _, rep = run(GOOD.replace("실습이다.", phrase))
            self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 1, phrase)
        for phrase in ("2026년에 나왔다.", "5분이면 된다.", "원칙 3원칙을 지킨다."):
            _, rep = run(GOOD.replace("실습이다.", phrase))
            self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 0, phrase)

    def test_citation_is_checked_per_sentence(self):
        # 결함: 같은 문단에 출처 태그가 하나만 있어도 다른 문장의 수치가 통과했다
        _, rep = run(GOOD.replace("**강조**는 마크다운으로 쓴다.", "월 구독료는 30달러다. **강조**는 마크다운으로 쓴다."))
        self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 1)

    def test_example_tag_exempts_illustrative_numbers(self):
        _, rep = run(GOOD.replace("실습이다.", "응답자는 387명이었다 [[예시]]."))
        self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 0)
        self.assertIn("예시 표시 수치", rep)

    def test_strict_numeric_level_is_red(self):
        toc = TOC.replace("수치 출처 필수: 예", "수치 출처 필수: 엄격")
        code, rep = run(GOOD.replace("실습이다.", "직원 300명이 썼다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "출처 없는 수치"), 1)

    def test_real_name_is_red(self):
        # 결함: '영업팀 김민수 과장은…' 같은 실명 일화를 잡지 못했다
        code, rep = run(GOOD.replace("실습이다.", "영업팀 김민수 과장은 이렇게 했다."))
        self.assertEqual((count(rep, "🔴", "실명"), count(rep, "🟡", "실명")), (0, 0), "기본은 점수에 넣지 않고 사실 확인 표로 넘긴다")
        self.assertIn("실명일 수 있는 표현", rep)
        code, rep = run(GOOD.replace("실습이다.", "영업팀 김민수 과장은 이렇게 했다."), toc=STRICT)
        self.assertEqual(count(rep, "🔴", "실명"), 1, "'엄격'이면 🔴로 출판을 막는다")
        for ok in ("최 팀장은 이렇게 했다.", "경쟁사 대표가 말했다.", "구매팀 과장이 말했다.", "지시는 팀장이 한다."):
            _, rep = run(GOOD.replace("실습이다.", ok))
            self.assertEqual(count(rep, "🔴", "실명"), 0, ok)

    def test_names_from_sources_are_caught_without_titles(self):
        # 결함: 사내 자료에 '박지훈 팀장'이 있어도 본문의 '박지훈과 이수진이'는 직함이 없어 그대로 출판됐다
        md = GOOD.replace("실습이다.", "박지훈과 이수진이 함께 맡았다.")
        src = {"memo.md": "담당: 박지훈 팀장, 이수진 대리"}
        _, rep = run_in_work(md, sources=src)
        self.assertEqual(count(rep, "🟡", "사내 자료에 나온 실명"), 2, rep)
        _, rep = run_in_work(md, toc=STRICT, sources=src)
        self.assertEqual(count(rep, "🔴", "사내 자료에 나온 실명"), 2, "엄격이면 막는다")
        _, rep = run_in_work(md, toc=STRICT.replace("- 실명 금지: 엄격", "- 실명 금지: 엄격\n- 실명 예외: 박지훈, 이수진"), sources=src)
        self.assertEqual(count(rep, "🔴", "실명"), 0, "실명 예외는 그대로 통한다")

    def test_figure_text_is_checked(self):
        # 결함: 그림 속 고객사 이름과 실명은 실명 검사, 금지 용어 검사 어디에도 들어가지 않았다
        fig = {"fig01": {"texts": ["대성모터스 영문 사양서", "박지훈 팀장"], "values": [3]}}
        toc = STRICT.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 커스텀 지시 → 맞춤 지시; 대성모터스")
        _, rep = run_in_work(GOOD, toc=toc, figdata=fig)
        self.assertEqual(count(rep, "🔴", "그림 fig01: '대성모터스'"), 1, rep)
        self.assertEqual(count(rep, "🔴", "그림 fig01: 박지훈 팀장"), 1, rep)

    def test_never_write_items_are_enforced(self):
        # 결함: README는 '쓰면 안 되는 것'을 검사 기준으로 쓴다고 했지만 읽는 코드가 없었다
        toc = TOC.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 커스텀 지시 → 맞춤 지시\n- 쓰면 안 되는 것: 인력 재배치, 고객사 이름")
        _, rep = run(GOOD.replace("실습이다.", "2027년 인력 재배치 계획을 세웠다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "쓰면 안 되는 것"), 1, rep)
        # 범주만 적고 금지 용어가 비어 있으면 구체 낱말을 적으라고 멈춘다
        toc2 = toc.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 없음")
        _, rep = run(GOOD, toc=toc2)
        self.assertEqual(count(rep, "🔴", "금지 용어가 비어 있음"), 1, rep)
        _, rep = run(GOOD, toc=toc2.replace("인력 재배치, 고객사 이름", "없음"))
        self.assertEqual(count(rep, "🔴", "금지 용어가 비어 있음"), 0)

    def test_anida_is_plain_style(self):
        # 결함: '아니다.'를 합쇼체로 세어 해라체 원고에 어미 혼용 🟡가 났다
        md = GOOD.replace("실습이다.", "정답이 아니다. 지름길도 아니다. 끝도 아니다. 학교에 다니다.")
        _, rep = run(md)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 0, rep)

    def test_korean_units_are_normalized(self):
        # 결함: 본문 '1만 5천 원'과 출처 기록 '15,000원'을 다른 수치로 보았다. '300여 명', '10조를'은 수치로 잡지 못했다
        self.assertEqual(verify.norm_num("1만 5천 원"), verify.norm_num("15,000원"))
        self.assertEqual(verify.norm_num("3천만 원"), "30000000원")
        self.assertEqual(verify.norm_num("1.5억 원"), "150000000원")
        self.assertEqual(verify.norm_num("300여 명"), "300명")
        for s in ("300여 명이 왔다.", "매출 10조를 넘었다.", "300위안이다.", "수천억 원을 썼다.", "3분의 1이 찬성했다."):
            self.assertTrue(verify.NUMERIC_CLAIM_RE.search(s), s)

    def test_crash_inputs_end_with_messages(self):
        # 결함: UTF-8이 아닌 원고와 path가 숫자인 출처 파일에서 traceback이 났다
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "15_manuscript.md").write_bytes("한글 원고".encode("cp949"))
            (d / "toc.md").write_text(TOC, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(verify.main([str(d / "15_manuscript.md"), "--toc", str(d / "toc.md")]), 2)
            (d / "01_citations.json").write_text('[{"id": "cite_001", "title": "t", "path": 123}]', encoding="utf-8")
            (d / "15_manuscript.md").write_text(GOOD, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIn(verify.main([str(d / "15_manuscript.md"), "--toc", str(d / "toc.md")]), (0, 1, 3))

    def test_leaks_in_code_blocks_citations_and_variants(self):
        # 결함: 프롬프트 예시(코드 블록), 책 끝 출처 제목, 띄어쓰기·대소문자를 바꾼 고객사명이 검사를 빠져 최종 PDF에 실렸다
        toc = STRICT.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 커스텀 지시 → 맞춤 지시; 대성모터스; Daesung Motors")
        md = GOOD.replace("실습이다.", "실습이다.\n\n```\n박지훈 팀장에게 보낼 대성 모터스 견적 요약\n```\n\n`DAESUNG MOTORS 견적`을 넣는다.")
        _, rep = run_in_work(md, toc=toc)
        self.assertEqual(count(rep, "🔴", "금지 용어"), 2, rep)
        self.assertEqual(count(rep, "🔴", "실명으로 보이는 인물"), 1, rep)
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "15_manuscript.md").write_text(GOOD, encoding="utf-8")
            (d / "toc.md").write_text(toc, encoding="utf-8")
            cites = (FIX / "01_citations.json").read_text(encoding="utf-8").replace('"title": "', '"title": "대성모터스 담당 ', 1)
            (d / "01_citations.json").write_text(cites, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                verify.main([str(d / "15_manuscript.md"), "--toc", str(d / "toc.md"), "--report", str(d / "r.md")])
            self.assertEqual(count((d / "r.md").read_text(encoding="utf-8"), "🔴", "출처 cite_"), 1)

    def test_source_names_in_many_shapes(self):
        # 결함: 자료 속 '최유리(팀장)', '| 강다은 | 대리 |', '생산관리팀 정민호'는 이름으로 모이지 않았고, 본문의 '지훈 씨'도 지나갔다.
        # 반대로 '지원자님', '정회원님'은 이름으로 모여 본문의 '지원자는'이 출판을 막았다
        src = {"org.md": "최유리(팀장)\n| 강다은 | 대리 |\n생산관리팀 정민호\n박지훈 팀장\n지원자님, 정회원님께 안내"}
        names, ok, failed = verify.source_names([], set())
        self.assertEqual((names, ok, failed), (set(), 0, []))
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "org.md"
            f.write_text(src["org.md"], encoding="utf-8")
            names, _, _ = verify.source_names([f], set())
        self.assertEqual(names, {"최유리", "강다은", "박지훈"})
        # '생산관리팀 정민호'는 '마케팅팀 전환율'과 모양이 같아 약한 근거로만 모은다 (사실 확인 표에 보여 주고 막지는 않는다)
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "org.md"
            f.write_text(src["org.md"] + "\n마케팅팀 전환율 개선", encoding="utf-8")
            people = verify.source_people([f], set())
        self.assertIn("정민호", people["weak"])
        self.assertNotIn("전환율", people["names"])
        md = GOOD.replace("실습이다.", "최유리와 강다은이 맡았다. 지훈 씨도 왔다. 지원자는 정회원과 다르다.")
        _, rep = run_in_work(md, toc=STRICT, sources=src)
        self.assertEqual(count(rep, "🔴", "사내 자료에 나온 실명"), 3, rep)
        # 자료에 있는 이름은 직함이 붙어도 기본 모드에서 🟡다 (근거가 더 강한 쪽이 더 약하게 판정되지 않게)
        _, rep = run_in_work(GOOD.replace("실습이다.", "박지훈 팀장이 정리했다."), sources=src)
        self.assertEqual(count(rep, "🟡", "사내 자료에 나온 실명"), 1, rep)

    def test_never_write_items_per_item(self):
        # 결함: 한 항목에 '없다'가 들어 있으면 검사 전체가 꺼졌고, 범주명('고객사 이름') 자체가 안내 문장에서 걸렸다.
        # '2027년 인력 재배치 계획'을 '내년 인력 재배치 계획'으로 쓰면 통과했다
        toc = TOC.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시",
                          "- 금지 용어: 커스텀 지시 → 맞춤 지시\n- 쓰면 안 되는 것: 오로라 프로젝트, 근거가 없다고 밝혀진 소문, 고객사 이름, 2027년 인력 재배치 계획")
        md = GOOD.replace("실습이다.", "오로라 프로젝트를 소개한다. 고객사 이름을 넣지 않는다. 내년 인력 재배치 계획을 세웠다.")
        _, rep = run(md, toc=toc)
        self.assertEqual(count(rep, "🔴", "쓰면 안 되는 것"), 2, rep)
        # 구체 낱말만 적었다면 금지 용어가 비어도 막지 않는다 (범주가 있을 때만 구체 낱말을 요구한다)
        toc2 = TOC.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 없음\n- 쓰면 안 되는 것: 오로라 프로젝트")
        _, rep = run(GOOD, toc=toc2)
        self.assertEqual(count(rep, "🔴", "금지 용어가 비어 있음"), 0, rep)

    def test_stale_figure_records_are_not_trusted(self):
        # 결함: 그림 파일을 다른 방법으로 덮어써도 옛 기록이 남아 새 그림의 실명을 놓쳤고, 원고에서 뺀 그림의 기록이 출판을 막았다
        fig = {"fig01": {"texts": ["자료"], "sha": "옛해시"}, "fig09": {"texts": ["대성모터스"]}}
        toc = TOC.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 커스텀 지시 → 맞춤 지시; 대성모터스")
        _, rep = run_in_work(GOOD, toc=toc, figdata=fig)
        self.assertEqual(count(rep, "🔴", "대성모터스"), 0, "원고에 없는 그림(fig09)은 검사하지 않는다")
        self.assertIn("검사기가 글자를 읽지 못한 그림", rep)

    def test_decimal_units_and_baek(self):
        # 결함: '2.3억 원'이 부동소수 오차로 '2억 3천만 원'과 달랐고, '2.0%'와 '2%'도 달랐다. '5백만 원'은 수치로 잡지 못했다
        self.assertEqual(verify.norm_num("2.3억 원"), verify.norm_num("2억 3천만 원"))
        self.assertEqual(verify.norm_num("2.0%"), verify.norm_num("2%"))
        self.assertEqual(verify.norm_num("5백만 원"), "5000000원")

    def test_emphasis_precheck_matches_converter(self):
        # 결함: '**별표 안 * 하나**'와 '**[[cite_001]]**'는 검사를 통과했지만 변환에서 🔴가 났다
        for bad, cat in (("**별표 안 * 하나** 문장이다.", "짝이 맞지 않는 **"), ("**강조 [[cite_001]]** 문장이다.", "굵은 글씨 안의 출처 태그")):
            _, rep = run(GOOD.replace("실습이다.", bad))
            self.assertEqual(count(rep, "🔴", cat), 1, bad)
        _, rep = run(GOOD.replace("실습이다.", "**굵게 *기울임* 섞기**와 ***둘 다***이다."))
        self.assertEqual(count(rep, "🔴", "**"), 0, rep)

    def test_read_any_cp949_and_docx_tables(self):
        # 결함: CP949 텍스트와 Word 표(조직도) 속 이름을 읽지 못해 대조에서 빠졌다
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "a.txt").write_bytes("박지훈 과장".encode("cp949"))
            self.assertIn("박지훈", verify.read_any(d / "a.txt"))
            try:
                from docx import Document
            except ImportError:
                return
            doc = Document()
            t = doc.add_table(rows=1, cols=2)
            t.rows[0].cells[0].text, t.rows[0].cells[1].text = "정민호", "팀장"
            doc.save(str(d / "b.docx"))
            names, ok, _ = verify.source_names([d / "a.txt", d / "b.docx", d / "c.hwp"], set())
            self.assertEqual(names, {"박지훈", "정민호"})
            self.assertEqual(ok, 2)

    def test_people_lists_emails_and_office_files(self):
        # 결함: '참석자: …' 목록, '작성자: …', 이메일 속 영문 이름, pptx·xlsx 자료의 이름을 모으지 못해 본문에서 그대로 나갔다
        src = {"minutes.md": "참석자: 박지훈, 최유리, 윤도현, 한예슬\n작성자: 오세린\n연락: jihoon.park@daesung.com"}
        md = GOOD.replace("실습이다.", "윤도현이 제안했다. 오세린이 정리했다. Jihoon Park이 검토했다.")
        _, rep = run_in_work(md, toc=STRICT, sources=src)
        self.assertEqual(count(rep, "🔴", "사내 자료에 나온 실명"), 2, rep)
        self.assertEqual(count(rep, "🔴", "영문 이름"), 1, rep)
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "s.pptx"
            with zipfile.ZipFile(f, "w") as z:
                z.writestr("ppt/slides/slide1.xml", "<p:sld><a:p><a:t>발표자: 송민재</a:t></a:p></p:sld>")
            self.assertIn("송민재", verify.source_people([f], set())["names"])

    def test_honorific_and_allow_variants(self):
        # 결함: 엄격 모드에서 '지원자님께'가 실명 🔴로 출판을 막았고, '김영호 대표이사'를 예외에 적어도 '김영호 대표'가 다시 걸렸다
        _, rep = run(GOOD.replace("실습이다.", "지원자님께 안내한다."), toc=STRICT)
        self.assertEqual(count(rep, "🔴", "실명"), 0, rep)
        toc = STRICT.replace("- 실명 금지: 엄격", "- 실명 금지: 엄격\n- 실명 예외: 김영호 대표이사")
        _, rep = run(GOOD.replace("실습이다.", "김영호 대표의 인사말이다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "실명") + count(rep, "🟡", "실명"), 0, rep)

    def test_citation_scope_matches_printed_list(self):
        # 결함: 검사기는 책에 찍히지 않는 claim을 막고, 찍히는 url은 보지 않아 변환 단계에서야 실패했다
        toc = STRICT.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시", "- 금지 용어: 커스텀 지시 → 맞춤 지시; daesungmotors")
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "15_manuscript.md").write_text(GOOD, encoding="utf-8")
            (d / "toc.md").write_text(toc, encoding="utf-8")
            cites = (FIX / "01_citations.json").read_text(encoding="utf-8")
            cites = cites.replace("https://example.com/official", "https://daesungmotors.com/a", 1).replace('"claim": "', '"claim": "박지훈 팀장 확인. ', 1)
            (d / "01_citations.json").write_text(cites, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                verify.main([str(d / "15_manuscript.md"), "--toc", str(d / "toc.md"), "--report", str(d / "r.md")])
            rep = (d / "r.md").read_text(encoding="utf-8")
        self.assertEqual(count(rep, "🔴", "daesungmotors"), 1, rep)
        self.assertEqual(count(rep, "🔴", "박지훈"), 0, "claim은 책에 찍히지 않는다 (사실 확인 표에서 본다)")

    def test_book_title_heading_is_red(self):
        # 결함: 원고 첫 줄의 책 제목 H1이 통과해 docx에 표지가 두 번 생겼다
        _, rep = run("# 테스트 책: 부제\n\n" + GOOD)
        self.assertEqual(count(rep, "🔴", "목차에 없는 부"), 1)

    def test_bracket_notes_in_toc_are_not_chapters(self):
        # 결함: 목차의 [주의], [Tip] 줄을 장으로 읽어 누락 🔴을 냈다
        toc = TOC.replace("- [실습 01] 첫 실습", "- [실습 01] 첫 실습\n- [주의] 이 부는 짧게\n- [Tip] 실습 팁")
        _, rep = run(GOOD, toc=toc)
        self.assertEqual(count(rep, "🔴", "목차 항목 누락"), 0, rep)

    def test_tier0_source_path_must_exist(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "15_manuscript.md").write_text(GOOD, encoding="utf-8")
            (d / "toc.md").write_text(TOC, encoding="utf-8")
            (d / "01_citations.json").write_text('[{"id": "cite_001", "tier": 0, "title": "사내 정책", "path": "user_input/sources/없는파일.pdf"}]', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                verify.main([str(d / "15_manuscript.md"), "--toc", str(d / "toc.md"), "--report", str(d / "r.md")])
            self.assertIn("출처 파일 경로 없음", (d / "r.md").read_text(encoding="utf-8"))

    def test_report_details_are_single_line_and_hashed(self):
        _, rep = run(GOOD.replace("실습이다.", "직원 300명이\n썼다."))
        self.assertTrue(re.search(r"^- 원고 해시: [0-9a-f]{12}$", rep, re.M))
        for line in rep.splitlines():
            if "출처 없는 수치" in line:
                self.assertTrue(line.startswith("- 🟡"), line)

    # ---- 3차 평가에서 나온 오탐·미탐 ----
    def test_honorific_names_and_false_positives(self):
        for bad in ("박서연 님이 말했다.", "이수진 씨는 웃었다.", "남궁민수 팀장이 왔다."):
            _, rep = run(GOOD.replace("실습이다.", bad), toc=STRICT)
            self.assertEqual(count(rep, "🔴", "실명"), 1, bad)
        for ok in ("이러한 사원이 많다.", "성실한 팀장이 있다.", "정규직 사원이 늘었다.", "고객님께 보낸다."):
            _, rep = run(GOOD.replace("실습이다.", ok))
            self.assertEqual(count(rep, "🔴", "실명"), 0, ok)

    def test_org_voice_forbids_first_person(self):
        toc = TOC.replace("담담한 설명 톤이다.", "담담한 설명 톤이다.\n\n## 저자 자료\n\n- 화자 성격: 조직 화자")
        _, rep = run(GOOD.replace("실습이다.", "나는 처음 이 도구를 썼다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "1인칭"), 1)
        _, rep = run(GOOD.replace("실습이다.", "내용을 보면 내부 자료가 있다. 그는 \"나는 몰랐다\"고 했다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "1인칭"), 0, "'내용', '내부', 인용문 속 1인칭은 제외")

    def test_repeated_number_is_grouped(self):
        # 결함: '팀 50명' 같은 같은 수치 23곳이 23건으로 세어져 점수를 독차지했다
        bad = GOOD.replace("실습이다.", "팀 50명이 썼다.\n\n다시 팀 50명이 썼다.\n\n또 팀 50명이다.")
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 1)
        self.assertIn("같은 수치 3곳", rep)

    def test_more_numeric_units(self):
        for phrase in ("매출은 120억이다.", "업계 1위다.", "가격은 USD 30이다."):
            _, rep = run(GOOD.replace("실습이다.", phrase))
            self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 1, phrase)
        for phrase in ("3명령어를 쓴다.", "5배치로 나눈다.", "3위안에 든다는 말은 없다."):
            _, rep = run(GOOD.replace("실습이다.", phrase))
            self.assertEqual(count(rep, "🟡", "출처 없는 수치"), 0, phrase)

    def test_toc_instruction_lines_are_not_chapters(self):
        toc = TOC.replace("- [실습 01] 첫 실습", "- [실습 01] 첫 실습\n- 3장 분량으로 쓴다\n- [표 1] 비교표를 넣는다\n- [Tip 1] 팁 상자")
        _, rep = run(GOOD, toc=toc)
        self.assertEqual(count(rep, "🔴", "목차 항목 누락"), 0, rep)

    def test_jyo_ending_and_bold_before_period(self):
        _, rep = run(GOOD.replace("실습이다.", "그렇죠. 쉽죠. 맞죠."))
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)
        _, rep = run(GOOD.replace("실습이다.", "**실습입니다**. **중요합니다**. **됩니다**."))
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 1)

    def test_number_not_in_cited_claim(self):
        # 결함: 출처 태그만 붙어 있으면 출처에 없는 수치도 통과했다
        _, rep = run(GOOD.replace("시장 점유율은 42%다 [[cite_001]].", "시장 점유율은 42%, 매출은 3억 원이다 [[cite_001]]."))
        self.assertEqual(count(rep, "🟡", "출처 기록에 없는 수치"), 1, rep)

    def test_org_voice_polite_first_person(self):
        toc = TOC.replace("담담한 설명 톤이다.", "담담한 설명 톤이다.\n\n## 저자 자료\n\n- 화자 성격: 조직 화자")
        _, rep = run(GOOD.replace("실습이다.", "저는 처음 써 봤다."), toc=toc)
        self.assertEqual(count(rep, "🔴", "1인칭"), 1)

    def test_tilde_fences_are_code(self):
        bad = GOOD.replace("```\n프롬프트 예시는", "~~~\n프롬프트 예시는").replace("그대로 둡니다.\n```", "그대로 둡니다.\n~~~")
        self.assertIn("~~~", bad)
        _, rep = run(bad)
        self.assertEqual(count(rep, "🟡", "어미 혼용"), 0, rep)

    def test_report_records_rules_hash(self):
        _, rep = run(GOOD)
        self.assertTrue(re.search(r"^- 책 설정 해시: [0-9a-f]{12}$", rep, re.M))

    # ---- 6차 평가 ----
    def test_public_titles_are_yellow_personal_titles_red(self):
        # 결함: 직함 목록에 '시장'을 넣자 '한국어 시장' 같은 말이 🔴로 출판을 막았다
        _, rep = run(GOOD.replace("실습이다.", "오세훈 시장이 말했다."), toc=STRICT)
        self.assertEqual((count(rep, "🔴", "실명"), count(rep, "🟡", "공인 실명")), (0, 1), "엄격이어도 공인은 🟡")
        _, rep = run(GOOD.replace("실습이다.", "김민수 과장이 말했다."), toc=STRICT)
        self.assertEqual(count(rep, "🔴", "실명"), 1)
        _, rep = run(GOOD.replace("실습이다.", "한국어 시장이 커졌다."), toc=STRICT)
        self.assertEqual(count(rep, "🔴", "실명") + count(rep, "🟡", "공인 실명"), 0)

    def test_line_numbers_survive_citation_tags(self):
        # 결함: 출처 태그를 지운 텍스트의 위치로 줄 번호를 계산해 1인칭·어미 위치가 수십 줄 앞으로 밀렸다
        toc = TOC.replace("담담한 설명 톤이다.", "담담한 설명 톤이다.\n\n## 저자 자료\n\n- 화자 성격: 조직 화자")
        filler = "\n".join(f"수치 {i}%다 [[cite_001]]." for i in range(30))
        md = GOOD.replace("첫 장이다.", filler + "\n\n첫 장이다.").replace("실습이다.", "저는 이렇게 했다.")
        _, rep = run(md, toc=toc)
        want = next(i for i, ln in enumerate(md.splitlines(), 1) if "저는 이렇게" in ln)
        self.assertIn(f"1인칭 표현 ({want}행)", rep)

    def test_page_estimate_uses_figure_density(self):
        # 글 위주 원고는 쪽당 글자가 많다 (실측: 그림 4개 83,363자 → 111쪽)
        toc = TOC.replace("약 2쪽 (약 300~3,000자)", "100페이지 (약 300~300,000자)")
        md = GOOD.replace("첫 장이다.", "첫 장이다. " + "가나다라마바사아자 " * 7000)
        per = {}
        for paper in ("B5", "A5", "A4"):
            _, rep = run(md, toc=toc.replace("- 판형: A5", f"- 판형: {paper}"))
            per[paper] = int(re.search(r"쪽당 약 (\d+)자", rep).group(1))
        self.assertGreater(per["B5"], 700)
        # 결함: 판형을 보지 않아 A4 책의 예상 쪽수가 실측(42쪽)보다 크게 나왔다
        self.assertTrue(per["A5"] < per["B5"] < per["A4"], per)

    def test_early_page_estimate(self):
        toc = TOC.replace("약 2쪽 (약 300~3,000자)", "150페이지 (약 300~300,000자)")
        _, rep = run(GOOD.replace("첫 장이다.", "첫 장이다. " + "가나다라마바사 " * 400), toc=toc)
        self.assertEqual(count(rep, "🟡", "예상 쪽수"), 1)


if __name__ == "__main__":
    unittest.main()
