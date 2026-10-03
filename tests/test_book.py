"""book.py 파이프라인 테스트. 임시 작업 폴더에서 책 한 권의 흐름을 끝까지 흉내 낸다.

게이트를 우회하려는 시도(형식만 맞춘 변경 로그, 고치지 않은 수정본, 순서 건너뛰기,
옛 원고 기준 리뷰)가 막히는지를 중심으로 본다.
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import _common as C  # noqa: E402
import book  # noqa: E402

FIX = HERE / "fixtures"
GOOD = (FIX / "15_manuscript.md").read_text(encoding="utf-8")
HEAD = "| 항목 | 심각도 | 출처 | 반영 | 사유 |\n|---|---|---|---|---|\n"


def parts_of(md):
    return [t.strip() for title, t in C.manuscript_divisions(C.strip_markers(md)) if title]


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        pathlib.Path("user_input").mkdir()
        shutil.copy(FIX / "toc.md", "user_input/user-book-toc.md")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def bk(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = book.main(list(args))
        return code, out.getvalue()

    def ok(self, *args):
        code, out = self.bk(*args)
        self.assertEqual(code, 0, out)
        return out

    def status(self):
        _, out = self.bk("status", "--json")
        return json.loads(out)

    @property
    def d(self):
        return book.active_dir()

    def write(self, name, text):
        (self.d / name).write_text(text, encoding="utf-8")

    def state(self):
        return json.loads((self.d / "_state.json").read_text(encoding="utf-8"))

    def log(self, sid, body):
        p = self.d / "_changelog.md"
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(old + f"\n## {sid}\n\n{body}\n", encoding="utf-8")

    def run_to_draft(self):
        self.ok("init", "--date", "20261003")
        shutil.copy(FIX / "01_citations.json", self.d / "01_citations.json")
        self.write("01_research-notes.md", "조사 내용")
        self.ok("complete", "01_research-notes")
        self.write("02_outline.md", "설계")
        self.ok("complete", "02_outline")
        self.ok("approve", "outline")
        for i, p in enumerate(parts_of(GOOD), 1):
            self.write(f"03_draft-v1_p{i:02d}.md", p)
            self.ok("complete", f"03_draft-v1_p{i:02d}")
            if i == 2:  # 톤 승인은 장이 있는 첫 부(제1부) 뒤
                self.ok("approve", "tone")
        return self.bk("merge", "03_draft-v1")

    def review(self, sid, red=0, yellow=0, body=None):
        lines = [f"# {sid}", ""]
        lines += [f"[🔴] [제1부] - 문제 {i}\n- 문제점: …" for i in range(red)]
        lines += [f"[🟡] [제2부] - 제안 {i}\n- 문제점: …" for i in range(yellow)]
        if not red and not yellow:
            lines.append("지적 없음")
        self.write(f"{sid}.md", body or "\n".join(lines))
        return self.bk("score", sid)

    def ensemble(self, sid, red=0, yellow=0, n=5):
        body = [f"# {sid}", "격리: 서브에이전트 5개"]
        for k in range(n):
            body.append(f"## 평가자 {k+1}: 평가자{k+1}")
            if k == 0:
                body += [f"[🔴] [제1부 제1장] - 문제 {i}" for i in range(red)]
                body += [f"[🟡] [전체] - 제안 {i}" for i in range(yellow)]
            if k > 0 or not (red or yellow):
                body.append("지적 없음")
        self.write(f"{sid}.md", "\n".join(body))
        return self.bk("score", sid)

    def red_ids(self, sid):
        return [f["id"] for f in self.state()["reviews"][sid]["findings"] if f["sev"] == "🔴"]

    def revise_p02(self, sid, marker="고쳤다"):
        p2 = parts_of(GOOD)[1].replace("첫 장이다.", f"첫 장이다. {marker}.")
        self.write(f"{sid}_p02.md", p2)
        self.ok("complete", f"{sid}_p02")


class TestStatus(Workspace):
    def test_template_toc_requires_interview(self):
        pathlib.Path("user_input/user-book-toc.md").write_text("- 제목: {{책 제목}}\n", encoding="utf-8")
        self.assertEqual(self.status()["action"], "interview")
        self.assertEqual(self.bk("init")[0], 1, "빈 템플릿으로는 책을 시작하지 않는다")

    def test_init_and_research(self):
        self.assertEqual(self.status()["action"], "init")
        self.ok("init", "--date", "20261003")
        s = self.status()
        self.assertEqual(s["stage"], "01_research-notes")
        self.assertIn("4단계 중 1단계(리서치)", s["progress"])
        self.ok("init", "--date", "20261003")
        self.assertEqual(self.d.name, "20261003_01")

    def test_partial_file_resumes_without_overwrite(self):
        self.ok("init", "--date", "20261003")
        self.write("01_research-notes.md", "절반")
        self.assertEqual(self.status()["action"], "resume")

    def test_active_folder_is_not_guessed(self):
        # 결함: 포인터가 없으면 가장 최신 책으로 조용히 바뀌었다
        self.ok("init", "--date", "20261001")
        self.ok("init", "--date", "20261002")
        pathlib.Path("draft/notes").mkdir()
        (pathlib.Path("draft") / ".active").unlink()
        self.assertIsNone(book.active_dir())
        self.assertEqual(self.status()["action"], "choose")
        self.assertEqual(self.bk("use", "notes")[0], 1, "책 폴더가 아닌 곳은 고를 수 없다")
        self.ok("use", "20261001")
        self.assertEqual(self.d.name, "20261001")

    def test_checkpoints_and_human_progress(self):
        self.ok("init", "--date", "20261003")
        self.write("01_research-notes.md", "조사")
        self.ok("complete", "01_research-notes")
        self.write("02_outline.md", "설계")
        self.ok("complete", "02_outline")
        self.assertEqual(self.status()["checkpoint"], "outline")
        self.ok("approve", "outline")
        s = self.status()
        self.assertEqual((s["action"], s["part"]), ("write-part", 1))
        self.assertIn("4단계 중 2단계(집필)", s["progress"])
        self.assertIn("전체의", s["progress"])
        self.write("03_draft-v1_p01.md", parts_of(GOOD)[0])
        self.ok("complete", "03_draft-v1_p01")
        self.assertEqual(self.status()["action"], "write-part", "프롤로그 뒤에는 톤 확인을 하지 않는다")
        self.write("03_draft-v1_p02.md", parts_of(GOOD)[1])
        self.ok("complete", "03_draft-v1_p02")
        self.assertEqual(self.status()["checkpoint"], "tone")
        self.write("03_draft-v1_p03.md", parts_of(GOOD)[2])
        self.assertEqual(self.bk("complete", "03_draft-v1_p03")[0], 1, "톤 승인 전에는 다음 부로 못 간다")

    def test_approval_invalidated_when_target_changes(self):
        self.ok("init", "--date", "20261003")
        self.write("01_research-notes.md", "조사")
        self.ok("complete", "01_research-notes")
        self.write("02_outline.md", "설계")
        self.ok("complete", "02_outline")
        self.ok("approve", "outline")
        (self.d / "02_outline.md").write_text("다른 설계\n\n<!-- STAGE_COMPLETE: 02_outline -->\n", encoding="utf-8")
        self.assertEqual(self.status()["checkpoint"], "outline")

    def test_order_is_enforced_outside_status(self):
        # 결함: 리서치·승인 없이 조각 완료, 초안 없이 리뷰 채점이 성공했다
        self.ok("init", "--date", "20261003")
        self.write("03_draft-v1_p02.md", parts_of(GOOD)[1])
        code, out = self.bk("complete", "03_draft-v1_p02")
        self.assertEqual(code, 1)
        self.write("04_review-red.md", "[🔴] [x] - y")
        code, out = self.bk("score", "04_review-red")
        self.assertEqual(code, 1)
        self.assertIn("03_draft-v1", out)

    def test_part_heading_must_match_toc(self):
        self.run_to_draft()
        self.review("04_review-red")
        self.write("05_draft-v2_p02.md", "# 엉뚱한 부\n\n내용")
        code, out = self.bk("complete", "05_draft-v2_p02")
        self.assertEqual(code, 1)
        self.assertIn("첫 줄", out)
        self.write("05_draft-v2_p02.md", parts_of(GOOD)[1] + "\n\n# 제2부: 응용\n\n중복")
        code, out = self.bk("complete", "05_draft-v2_p02")
        self.assertIn("2개", out)


class TestShortcuts(Workspace):
    def test_hand_marked_manuscript_cannot_be_approved(self):
        # 결함: 리서치 단계에서 15_manuscript.md에 완료 표시만 붙이면 사실 확인 승인과 변환이 통과했다
        self.ok("init", "--date", "20261003")
        self.write("15_manuscript.md", GOOD)
        import verify
        out = pathlib.Path("output") / self.d.name
        with contextlib.redirect_stdout(io.StringIO()):
            verify.main([str(self.d / "15_manuscript.md"), "--report", str(out / "verify-report.md")])
        code, msg = self.bk("approve", "facts")
        self.assertEqual(code, 1)
        self.assertIn("리뷰를 건너뛴", msg)

    def test_weighted_progress(self):
        # 결함: 240쪽 초안을 다 쓴 직후 진행률이 17%로 보였다
        self.run_to_draft()
        self.assertIn("전체의 약 46%", self.status()["progress"])


class TestExportImport(Workspace):
    def test_roundtrip_continues_in_a_new_folder(self):
        # Claude 앱과 ChatGPT는 대화창마다 파일이 사라질 수 있다. 작업 묶음으로 이어 간다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        before = self.status()
        out = self.ok("export", "--to", "outbox")
        zpath = next(pathlib.Path("outbox").glob("book-work-*.zip")).resolve()
        self.assertIn(zpath.name, out)
        other = pathlib.Path(tempfile.mkdtemp())
        try:
            os.chdir(other)
            self.ok("import", str(zpath))
            after = self.status()
            self.assertEqual((after["stage"], after["action"]), (before["stage"], before["action"]))
            self.assertEqual(self.bk("import", str(zpath))[0], 1, "같은 책을 두 번 풀어 덮어쓰지 않는다")
            self.assertTrue(any(e["event"] == "import" for e in self.state()["log"]))
        finally:
            os.chdir(self.tmp)
            shutil.rmtree(other)

    def test_import_never_overwrites_another_book(self):
        # 결함: 조작한 ZIP이 다른 책 폴더의 파일을 덮어쓰고 .active를 바꿨다
        import zipfile
        self.ok("init", "--date", "20261003")
        victim = self.d / "02_outline.md"
        victim.write_text("원래 내용", encoding="utf-8")
        with zipfile.ZipFile("evil.zip", "w") as z:
            z.writestr("draft/.active", "20261004\n")
            z.writestr("draft/20261004/_state.json", "{}")
            z.writestr("draft/20261003/02_outline.md", "덮어쓴 내용")
        self.assertEqual(self.bk("import", "evil.zip")[0], 1)
        self.assertEqual(victim.read_text(encoding="utf-8"), "원래 내용")
        self.assertEqual(book.active_dir().name, "20261003")

    def test_status_suggests_import_when_a_bundle_is_present(self):
        # 결함: 빈 폴더에 작업 묶음 ZIP이 있어도 status가 인터뷰를 안내했다
        self.run_to_draft()
        self.ok("export", "--to", ".")
        zpath = next(pathlib.Path(".").glob("book-work-*.zip")).resolve()
        other = pathlib.Path(tempfile.mkdtemp())
        try:
            os.chdir(other)
            shutil.copy(zpath, other / zpath.name)
            self.assertEqual(self.status()["action"], "import")
        finally:
            os.chdir(self.tmp)
            shutil.rmtree(other)

    def test_resume_without_bundle_asks_for_zip(self):
        # 결함: 앱에서 ZIP 없이 "이어서 써줘"라고 하면 첫 인터뷰를 다시 시작해 책이 사라진 것처럼 보였다
        shutil.rmtree(pathlib.Path("user_input"), ignore_errors=True)
        s = self.status()
        self.assertEqual(s["action"], "interview")
        self.assertIn("작업 묶음", s["next"])

    def test_legacy_book_folder_still_works(self):
        # 입력 폴더 이름을 book/에서 user_input/으로 바꿨다. 예전 작업 폴더도 그대로 이어 쓴다
        shutil.move("user_input", "book")
        self.assertNotEqual(self.status()["action"], "interview")

    def test_import_rejects_unsafe_paths(self):
        import zipfile
        with zipfile.ZipFile("evil.zip", "w") as z:
            z.writestr("draft/.active", "20261003\n")
            z.writestr("../outside.txt", "x")
        code, out = self.bk("import", "evil.zip")
        self.assertEqual(code, 1)
        self.assertFalse(pathlib.Path("../outside.txt").exists())


class TestScore(Workspace):
    def test_reviewer_does_not_write_own_score(self):
        self.run_to_draft()
        code, out = self.bk("complete", "04_review-red")
        self.assertEqual(code, 1)
        self.review("04_review-red", red=1, yellow=2)
        text = (self.d / "04_review-red.md").read_text(encoding="utf-8")
        self.assertIn("| 비평가 리뷰 | 1 | 2 | 0 | 7 |", text)
        self.assertIn("[🔴] [R04-01]", text)
        self.assertEqual(self.red_ids("04_review-red"), ["R04-01"])

    def test_finding_format_variants_are_counted(self):
        # 결함: '- [🔴]', '**[🔴]**', '1. 🔴' 형식은 0건으로 세어 10점이 나왔다
        self.run_to_draft()
        body = "# 리뷰\n\n- [🔴] [제1부] - 하나\n**[🔴]** [제2부] - 둘\n1. 🟡 [다] - 셋\n> [🟡] [라] - 넷\n### [🔴] [프롤로그] - 다섯\n- [필수] [부록 A] - 여섯\n"
        self.assertEqual(self.review("04_review-red", body=body)[0], 0)
        rec = self.state()["reviews"]["04_review-red"]
        self.assertEqual((rec["red"], rec["yellow"]), (4, 2))

    def test_findings_in_tables_or_code_blocks_refuse_scoring(self):
        # 결함: 표나 코드 블록으로 쓴 지적이 0건으로 집계되어 10점이 나왔다
        self.run_to_draft()
        for body in ("# 리뷰\n\n| 심각도 | 위치 |\n|---|---|\n| 🔴 | 제1부 |\n",
                     "# 리뷰\n\n```\n[🔴] [제1부] - 수치 오류\n```\n"):
            code, out = self.review("04_review-red", body=body)
            self.assertEqual(code, 1, body)
            self.assertIn("지적 형식에 맞지 않는", out)

    def test_zero_findings_need_explicit_note(self):
        self.run_to_draft()
        code, out = self.review("04_review-red", body="# 리뷰\n\n[심각] 제1부 수치 오류\n")
        self.assertEqual(code, 1, "형식이 어긋난 심각도 줄은 거부한다")
        code, out = self.review("04_review-red", body="# 리뷰\n\n제1부는 지적 없음. 제2부는 논리 비약이 있다.\n")
        self.assertEqual(code, 1, "'지적 없음'은 단독 줄일 때만 인정하고, 지적 섹션의 자유 문장은 거부한다")
        self.assertEqual(self.review("04_review-red", body="# 리뷰\n\n지적 없음\n")[0], 0)

    def test_red_needs_a_location(self):
        self.run_to_draft()
        code, out = self.review("04_review-red", body="# 리뷰\n\n[🔴] [어딘가] - 문제\n")
        self.assertEqual(code, 1)
        self.assertIn("위치", out)
        self.assertEqual(self.review("04_review-red", body="# 리뷰\n\n[🔴] [제1장 둘째 문단] - 문제\n")[0], 0)
        self.assertEqual(self.state()["reviews"]["04_review-red"]["findings"][0]["part"], [2], "장 이름으로 부를 찾는다")

    def test_ensemble_finding_outside_reviewer_section(self):
        self.run_to_draft()
        self.review("04_review-red")
        self.log("05_draft-v2", "변경 없음")
        self.ok("merge", "05_draft-v2")
        self.review("06_review-pink")
        self.log("07_draft-v3", "변경 없음")
        self.ok("merge", "07_draft-v3")
        body = "# 합평\n격리: 서브에이전트 5개\n[🔴] [제1부] - 앞에 쓴 지적\n" + "".join(f"## 평가자 {k}: 이름\n지적 없음\n" for k in range(1, 6))
        self.write("08_ensemble-review-1.md", body)
        code, out = self.bk("score", "08_ensemble-review-1")
        self.assertEqual(code, 1)
        self.assertIn("평가자 1 앞", out)
        self.write("08_ensemble-review-1.md", body.replace("격리: 서브에이전트 5개\n[🔴] [제1부] - 앞에 쓴 지적\n", ""))
        code, out = self.bk("score", "08_ensemble-review-1")
        self.assertIn("격리", out)

    def test_stray_severity_symbols_refuse_scoring(self):
        self.run_to_draft()
        code, out = self.review("04_review-red", body="# 리뷰\n\n문제: 🔴 근거 없는 수치가 있다\n")
        self.assertEqual(code, 1)
        self.assertIn("지적 형식에 맞지 않는", out)

    def test_rescoring_keeps_ids_stable(self):
        self.run_to_draft()
        self.review("04_review-red", red=2)
        self.ok("score", "04_review-red")
        text = (self.d / "04_review-red.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("[R04-01]"), 1)
        self.assertEqual(text.count("## 점수 (book.py 계산)"), 1)

    def test_partial_whole_words_still_map_to_part(self):
        # 결함: '[제1부 전반]'처럼 '전반'이 들어간 위치가 책 전체('*')로 처리되어 부 대응 검사가 꺼졌다
        self.run_to_draft()
        self.review("04_review-red", body="# 리뷰\n\n[🔴] [제1부 전반] - 흐름\n[🔴] [제1장 전체 흐름] - 구성\n[🔴] [전체] - 용어\n")
        parts = [f["part"] for f in self.state()["reviews"]["04_review-red"]["findings"]]
        self.assertEqual(parts, [[2], [2], "*"])

    def test_text_severity_lines_refuse_scoring(self):
        # 결함: '- 필수: …', '(심각도 높음) …' 줄이 0건으로 집계되었다
        self.run_to_draft()
        for body in ("# 리뷰\n\n- 필수: [제1부] 논리 비약\n", "# 리뷰\n\n(심각도 높음) [제1부] 논리 비약\n",
                     "# 리뷰\n\n| 심각도 | 위치 |\n|---|---|\n| [필수] | 제1부 |\n"):
            self.assertEqual(self.review("04_review-red", body=body)[0], 1, body)

    def test_more_severity_variants_refuse_scoring(self):
        # 결함: '- High:', '- 우선순위: 높음', 'P0:', '**치명적 문제**:', '[HIGH PRIORITY]', 표의 '**높음**', 🚨가 0건으로 빠졌다
        self.run_to_draft()
        for line in ("- High: 제1부 논리", "- 우선순위: 높음 제1부", "P0: 제1부 논리", "**치명적 문제**: 제1부",
                     "[HIGH PRIORITY] 제1부", "| 제1부 | **높음** |", "🚨 제1부 논리"):
            code, out = self.review("04_review-red", body="# 리뷰\n\n[🟡] [제1부] - 정상 지적\n" + line + "\n")
            self.assertEqual(code, 1, line)

    def test_severity_judgement_is_precise(self):
        # 정상 문장은 거부하지 않고, 새 변형은 거부한다
        self.run_to_draft()
        ok_lines = ("- 중간 점검: 1부 흐름을 다시 본다", "- Low-hanging fruit: 쉬운 개선", "제1장(중간 부분)은 좋다", "- 긴급 상황 대응 장: 잘 썼다")
        for line in ok_lines:
            body = "# 리뷰\n\n## 지적 사항\n[🟡] [제1부] - 정상 지적\n- 문제점: 흐름\n\n## 총평\n" + line + "\n"
            self.assertEqual(self.review("04_review-red", body=body)[0], 0, line)
        # 지적 섹션의 자유 문장은 형식 밖 지적일 수 있어 거부한다
        self.assertEqual(self.review("04_review-red", body="# 리뷰\n\n[🟡] [제1부] - 정상 지적\n제2부 논리가 약하다\n")[0], 1)
        bad_lines = ("- 중대: 제1부 논리", "Must fix: 제1부", "[MUST] 제1부", "- 심각도 = 높음", "- 고위험: 제1부", "🟥 제1부", "★★★ 제1부", "## 큰 문제: 제1부")
        for line in bad_lines:
            self.assertEqual(self.review("04_review-red", body="# 리뷰\n\n[🟡] [제1부] - 정상 지적\n" + line + "\n")[0], 1, line)

    def test_review_format_holes_are_closed(self):
        # 결함: 지적 섹션의 '### 헤딩 + 하위 항목', 홀로 쓴 하위 항목, 코드 블록, 비슷한 이름의 섹션으로 지적이 빠졌다
        self.run_to_draft()
        head = "# 리뷰\n\n## 지적 사항\n[🟡] [제1부] - 정상 지적\n- 문제점: 흐름\n\n"
        for bad in ("### 2. 제1부 제1장 통계 출처 없음\n- 문제점: 출판 불가\n",
                    "## 추가 지적\n- 문제점: 지적 줄 없이 홀로 쓴 하위 항목\n",
                    "```\n제1부 수치 오류\n```\n",
                    "## 요약 및 추가 지적\n제1부 논리가 무너졌다\n"):
            self.assertEqual(self.review("04_review-red", body=head + bad)[0], 1, bad)
        # 머리말, 구분선, '- 제안:', 이어지는 줄은 정상으로 본다
        ok = ("대상: 03_draft-v1.md\n\n## 지적 사항\n[🟡] [제1부] - 정상 지적\n- 제안: 문장을 줄인다\n"
              "  이어지는 설명 줄\n- 수정안: 다른 표현\n\n---\n\n## 총평\n전체적으로 좋다\n")
        self.assertEqual(self.review("04_review-red", body="# 리뷰\n\n" + ok)[0], 0)

    def test_more_review_format_holes(self):
        # 결함: 점수 섹션 뒤에 붙인 🔴 지적이 다시 채점할 때 잘려 나갔고,
        # 지적 섹션의 '# 제목', 들여 쓴 '### 헤딩', '격리:' 줄에 쓴 지적은 집계에서 빠졌다
        self.run_to_draft()
        self.review("04_review-red", yellow=1)
        f = self.d / "04_review-red.md"
        f.write_text(f.read_text(encoding="utf-8").rstrip() + "\n[🔴] [제1부 제1장] - 뒤에 붙인 지적\n", encoding="utf-8")
        code, out = self.bk("score", "04_review-red")
        self.assertEqual(code, 1, out)
        self.assertIn("점수 섹션", out)
        head = "# 리뷰\n\n## 지적 사항\n[🟡] [제1부] - 정상 지적\n- 문제점: 흐름\n"
        for bad in ("# 제1부 제1장 출처 없는 가격 수치. 출판 불가\n- 문제점: 출처 없음\n",
                    "  ### 제1부 수치 오류\n",
                    "격리: 제1부 수치 63%는 출처와 다르다\n"):
            self.assertEqual(self.review("04_review-red", body=head + bad)[0], 1, bad)

    def test_score_block_must_be_untouched(self):
        # 결함: 점수 섹션 뒤의 표 행('| [🔴] [제1부] - 통계 오류 |…')과 '- 판정:' 줄에 쓴 지적이 허용 목록을 통과했다
        self.run_to_draft()
        self.review("04_review-red", yellow=1)
        f = self.d / "04_review-red.md"
        orig = f.read_text(encoding="utf-8")
        for extra in ("| [🔴] [제1부] - 통계 오류 | 1 | 0 | 0 | 7 |", "- 판정: 제1부 통계가 틀려 출판 불가"):
            f.write_text(orig.replace("<!-- STAGE_COMPLETE", extra + "\n<!-- STAGE_COMPLETE"), encoding="utf-8")
            self.assertEqual(self.bk("score", "04_review-red")[0], 1, extra)
        f.write_text(orig, encoding="utf-8")
        self.assertEqual(self.bk("score", "04_review-red")[0], 0, "book.py가 쓴 그대로면 다시 채점할 수 있다")

    def test_location_variants_and_ranges(self):
        self.run_to_draft()
        body = "# 리뷰\n\n[🔴]: [제1부] - 콜론 뒤 위치\n**[필수]** 제2부 실습 - 괄호 없는 위치\n[🔴] [프롤로그~제2부] - 범위\n"
        self.assertEqual(self.review("04_review-red", body=body)[0], 0)
        parts = [f["part"] for f in self.state()["reviews"]["04_review-red"]["findings"]]
        self.assertEqual(parts, [[2], [3], [1, 2, 3]])
        self.write("04_review-red.md", "# 리뷰\n\n[🔴] [프롤로그-부록] - 대시 범위\n[🔴] [제1부에서 부록까지] - 말 범위\n")
        self.ok("score", "04_review-red", "--reason", "범위 표기 확인")
        parts = [f["part"] for f in self.state()["reviews"]["04_review-red"]["findings"]]
        self.assertEqual(parts, [[1, 2, 3, 4], [2, 3, 4]])

    def test_each_ensemble_reviewer_needs_findings_or_note(self):
        # 결함: 평가자 1만 지적하고 2~5가 형식 밖으로 쓰면 각자 10점이 되어 게이트를 통과했다
        self.run_to_draft()
        for rev, ver in [("04_review-red", "05_draft-v2"), ("06_review-pink", "07_draft-v3")]:
            self.review(rev)
            self.log(ver, "변경 없음")
            self.ok("merge", ver)
        body = "# 합평\n격리: 서브에이전트 5개\n## 평가자 1: 가\n[🟡] [제1부] - 제안\n" + "".join(
            f"## 평가자 {k}: 나\n논리 비약이 있다 [제1부].\n" for k in range(2, 6))
        self.write("08_ensemble-review-1.md", body)
        code, out = self.bk("score", "08_ensemble-review-1")
        self.assertEqual(code, 1, "평가자 2~5의 형식 밖 지적은 거부된다")
        body2 = "# 합평\n격리: 서브에이전트 5개\n## 평가자 1: 가\n[🟡] [제1부] - 제안\n" + "".join(f"## 평가자 {k}: 나\n" for k in range(2, 6))
        self.write("08_ensemble-review-1.md", body2)
        code, out = self.bk("score", "08_ensemble-review-1")
        self.assertIn("평가자 2", out, "빈 평가자 섹션도 거부된다")

    def test_rescoring_after_input_change_is_refused(self):
        # 결함: 원고가 바뀐 뒤 같은 리뷰를 재채점하면 '다시 리뷰하라' 신호가 꺼졌다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        p = self.d / "03_draft-v1.md"
        p.write_text(p.read_text(encoding="utf-8").replace("첫 장이다.", "첫 장이다. 바뀜."), encoding="utf-8")
        code, out = self.bk("score", "04_review-red")
        self.assertEqual(code, 1)
        self.assertIn("reset", out)

    def test_dropping_red_on_rescore_needs_reason(self):
        self.run_to_draft()
        self.review("04_review-red", red=2)
        code, out = self.review("04_review-red", red=1)
        self.assertEqual(code, 1)
        self.assertIn("--reason", out)
        self.ok("score", "04_review-red", "--reason", "중복 지적 제거")
        self.assertEqual(self.state()["log"][-1]["reason"], "중복 지적 제거")

    def test_ensemble_needs_reviewers_1_to_5(self):
        self.run_to_draft()
        self.review("04_review-red")
        self.log("05_draft-v2", "변경 없음")
        self.ok("merge", "05_draft-v2")
        self.review("06_review-pink")
        self.log("07_draft-v3", "변경 없음")
        self.ok("merge", "07_draft-v3")
        code, out = self.ensemble("08_ensemble-review-1", n=4)
        self.assertEqual(code, 1)
        self.assertEqual(self.ensemble("08_ensemble-review-1", red=1)[0], 0)
        self.assertEqual(self.red_ids("08_ensemble-review-1"), ["R08-1-01"])


class TestMergeGates(Workspace):
    def test_draft_merge(self):
        code, out = self.run_to_draft()
        self.assertEqual(code, 0, out)
        text = (self.d / "03_draft-v1.md").read_text(encoding="utf-8")
        self.assertTrue(text.rstrip().endswith("<!-- STAGE_COMPLETE: 03_draft-v1 -->"))
        self.assertNotIn("_p01 -->", text)
        self.assertEqual(self.status()["stage"], "04_review-red")

    def test_changelog_must_reference_every_red_id_with_check(self):
        # 결함: 행 개수만 세서, 같은 행 복사·빈 반영 칸·'부분'도 통과했다
        self.run_to_draft()
        self.review("04_review-red", red=2)
        self.revise_p02("05_draft-v2")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertIn("_changelog.md", out)
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n| 가 R04-01 | 🔴 | 04 | ✅ | |\n")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertIn("R04-02", out, "같은 행을 두 번 써도 두 번째 ID는 채워지지 않는다")
        (self.d / "_changelog.md").unlink()
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n| 나 R04-02 | 🔴 | 04 | 부분 | |\n")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertIn("✅가 아닙니다", out)
        (self.d / "_changelog.md").unlink()
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n| 나 R04-02 | 🔴 | 04 | ✅ | |\n| 다 R04-09 | 🟡 | 04 | ✅ | |\n")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertIn("리뷰에 없는 지적 ID", out)
        (self.d / "_changelog.md").unlink()
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n| 나 R04-02 | 🔴 | 04 | ✅ | |\n")
        self.ok("merge", "05_draft-v2")
        merged = (self.d / "05_draft-v2.md").read_text(encoding="utf-8")
        self.assertIn("고쳤다", merged)
        self.assertIn("# 프롤로그: 시작하며", merged)

    def test_red_requires_an_actual_rewrite(self):
        # 결함: 조각을 하나도 쓰지 않고 변경 로그만으로 병합되어, 실제 수정 0회로 완료까지 갔다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1)
        self.assertIn("고쳐 쓴 조각", out)
        self.write("05_draft-v2_p02.md", parts_of(GOOD)[1])
        self.ok("complete", "05_draft-v2_p02")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1)
        self.assertIn("고치지 않았습니다", out, "기준 원고와 같은 조각은 고친 것이 아니다")

    def test_red_must_be_fixed_in_the_part_it_points_to(self):
        # 결함: 🔴이 제1부에 있는데 프롤로그에 공백 한 칸만 넣어도 병합되었다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        self.log("05_draft-v2", HEAD + "| R04-01 | 🔴 | 04 | ✅ | |\n")
        self.write("05_draft-v2_p01.md", parts_of(GOOD)[0] + " ")
        self.ok("complete", "05_draft-v2_p01")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1)
        self.assertIn("p02", out)
        self.write("05_draft-v2_p02.md", parts_of(GOOD)[1].replace("첫 장이다.", "첫 장이다.  "))
        self.ok("complete", "05_draft-v2_p02")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1, "공백만 바꾼 것은 고친 것이 아니다")
        self.write("05_draft-v2_p02.md", parts_of(GOOD)[1].replace("첫 장이다.", "첫 장이다. 고쳤다."))
        self.bk("complete", "05_draft-v2_p02")
        (self.d / "05_draft-v2_p02.md").write_text(parts_of(GOOD)[1].replace("첫 장이다.", "첫 장이다. 고쳤다.") + "\n\n<!-- STAGE_COMPLETE: 05_draft-v2_p02 -->\n", encoding="utf-8")
        self.ok("merge", "05_draft-v2")

    def test_check_mark_must_be_exact(self):
        # 결함: 반영 칸이 '✅ 아님', '✅ 미반영(다음 판)'이어도 통과했다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        self.revise_p02("05_draft-v2")
        for cell in ("✅ 아님", "✅ 미반영(다음 판)"):
            self.log("05_draft-v2", HEAD + f"| R04-01 | 🔴 | 04 | {cell} | |\n")
            self.assertEqual(self.bk("merge", "05_draft-v2")[0], 1, cell)
        self.log("05_draft-v2", HEAD + "| R04-01 | 🔴 | 04 | ✅ 반영 | |\n")
        self.ok("merge", "05_draft-v2")

    def test_changelog_parsing_details(self):
        self.run_to_draft()
        self.review("04_review-red", red=2)
        self.revise_p02("05_draft-v2")
        # 사유 칸의 ID는 그 행의 대상이 아니다, 반영 칸은 ✅로 시작해야 한다
        self.log("05_draft-v2", HEAD + "| R04-01 | 🔴 | 04 | ✅ | |\n| R04-02 | 🔴 | 04 | ❌ 반영 안 함 (✅ 아님) | |\n")
        self.assertIn("✅가 아닙니다", self.bk("merge", "05_draft-v2")[1])
        # 같은 섹션을 덧붙여 고치면 마지막 섹션을 읽는다
        self.log("05_draft-v2", HEAD + "| R04-01 | 🔴 | 04 | ✅ | |\n| R04-02 | 🔴 | 04 | ✅ | R04-01 수정으로 해소 |\n")
        self.ok("merge", "05_draft-v2")

    def test_reset_retires_changelog_and_outputs(self):
        # 결함: reset 뒤 새 리뷰의 같은 ID(R04-01)에 옛 변경 로그 ✅가 쓰였다
        self.run_to_draft()
        self.review("04_review-red", red=1)
        self.revise_p02("05_draft-v2")
        self.log("05_draft-v2", HEAD + "| R04-01 | 🔴 | 04 | ✅ | |\n")
        self.ok("merge", "05_draft-v2")
        self.ok("reset", "04_review-red")
        self.assertNotIn("## 05_draft-v2\n", (self.d / "_changelog.md").read_text(encoding="utf-8"))
        self.review("04_review-red", red=1)
        self.revise_p02("05_draft-v2", marker="다시 고쳤다")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1, "옛 섹션으로는 통과하지 않는다")
        self.assertTrue(any(e["event"] == "reset-review" for e in self.state()["log"]))

    def test_toc_change_blocks_until_accepted(self):
        self.run_to_draft()
        toc = pathlib.Path("user_input/user-book-toc.md")
        toc.write_text(toc.read_text(encoding="utf-8").replace("검증기 회귀 테스트용", "다른 책"), encoding="utf-8")
        self.write("04_review-red.md", "지적 없음")
        code, out = self.bk("score", "04_review-red")
        self.assertEqual(code, 1)
        self.assertIn("accept-toc", out)
        self.ok("accept-toc")
        self.ok("score", "04_review-red")

    def test_merge_runs_verify_and_blocks_red(self):
        self.run_to_draft()
        self.review("04_review-red", red=1)
        self.revise_p02("05_draft-v2", marker="커스텀 지시를 쓴다")
        self.log("05_draft-v2", HEAD + "| 가 R04-01 | 🔴 | 04 | ✅ | |\n")
        code, out = self.bk("merge", "05_draft-v2")
        self.assertEqual(code, 1)
        self.assertIn("금지 용어", (self.d / "_verify-05_draft-v2.md").read_text(encoding="utf-8"))
        self.assertFalse(book.has_marker(self.d / "05_draft-v2.md"), "🔴이 있으면 완료로 두지 않는다")

    def test_stale_review_after_input_changes(self):
        # 결함: 리뷰 뒤에 원고를 다시 병합해도 그대로 다음 단계로 갔다
        self.run_to_draft()
        self.review("04_review-red")
        p = self.d / "03_draft-v1.md"
        p.write_text(p.read_text(encoding="utf-8").replace("첫 장이다.", "첫 장이다. 바뀜."), encoding="utf-8")
        s = self.status()
        self.assertEqual((s["action"], s["stage"]), ("stale", "04_review-red"))
        self.ok("reset", "04_review-red")
        self.assertFalse((self.d / "04_review-red.md").exists())
        self.assertEqual(self.status()["stage"], "04_review-red")

    def advance_to_10(self, red10):
        self.run_to_draft()
        for rev, ver in [("04_review-red", "05_draft-v2"), ("06_review-pink", "07_draft-v3")]:
            self.review(rev)
            self.log(ver, "변경 없음")
            self.ok("merge", ver)
        self.ensemble("08_ensemble-review-1")
        self.log("09_draft-v4", "변경 없음")
        self.ok("merge", "09_draft-v4")
        self.ensemble("10_ensemble-review-2", red=red10)

    def test_ensemble_gate_and_third_round(self):
        self.advance_to_10(red10=1)
        self.assertEqual(self.status()["action"], "next-round")
        self.ok("next-round")
        s = self.status()
        self.assertEqual(s["stage"], "09_draft-v4")
        self.assertTrue(s["inputs"]["base"].startswith("_backup/"))
        self.revise_p02("09_draft-v4")
        self.log("09_draft-v4", HEAD + "| 가 R10-1-01 | 🔴 | 10 | ✅ | |\n")
        self.ok("merge", "09_draft-v4")
        self.ensemble("10_ensemble-review-2", red=1)
        self.assertEqual(self.state()["ensemble"]["round"], 3)
        s = self.status()
        self.assertEqual(s["stage"], "11_draft-final")
        self.assertIn("미통과", s["warning"])
        self.assertEqual(self.bk("next-round")[0], 1, "4라운드는 없다")

    def test_third_round_rescoring_is_not_a_false_alarm(self):
        # 결함: 3라운드를 정상적으로 채점해도(🔴 2 → 0) '--reason'을 요구했다
        self.advance_to_10(red10=2)
        self.ok("next-round")
        self.revise_p02("09_draft-v4")
        self.log("09_draft-v4", HEAD + "| R10-1-01 | 🔴 | 10 | ✅ | |\n| R10-1-02 | 🔴 | 10 | ✅ | |\n")
        self.ok("merge", "09_draft-v4")
        code, out = self.ensemble("10_ensemble-review-2", red=0)
        self.assertEqual(code, 0, out)
        self.assertIn("10_ensemble-review-2@r2", self.state()["reviews"], "2라운드 기록은 보관된다")

    def test_publish_gates(self):
        self.advance_to_10(red10=0)
        self.log("11_draft-final", "변경 없음")
        self.ok("merge", "11_draft-final")
        for sid in ("12_review-editor", "13_review-marketer", "14_review-proofreader"):
            self.review(sid)
        self.log("15_manuscript", "변경 없음")
        self.ok("merge", "15_manuscript")
        s = self.status()
        self.assertEqual((s["action"], s["done"]), ("verify", len(book.STAGES)))
        self.assertIn("4단계 중 4단계(출판)", s["progress"])
        return s

    def test_end_to_end_publish(self):
        try:
            import build_docx
            import figure_kit
            import verify
        except ImportError as e:
            self.skipTest(f"모듈 없음: {e}")
        if build_docx.Document is None:
            self.skipTest("python-docx 없음")
        self.test_publish_gates()
        out = pathlib.Path("output") / self.d.name
        ms = self.d / "15_manuscript.md"
        self.assertEqual(self.bk("approve", "facts")[0], 1, "검사 전에는 사실 확인을 승인할 수 없다")
        # 결함: 출판 직전에 검증 규칙을 지우고 다시 검사하면 통과했다
        toc = pathlib.Path("user_input/user-book-toc.md")
        orig = toc.read_text(encoding="utf-8")
        toc.write_text(orig.replace("- 금지 용어: 커스텀 지시 → 맞춤 지시\n", ""), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            verify.main([str(ms), "--report", str(out / "verify-report.md")])
        self.assertEqual(self.status()["action"], "accept-toc", "바뀐 규칙으로 한 검사는 인정하지 않는다")
        toc.write_text(orig, encoding="utf-8")
        ms.write_text(ms.read_text(encoding="utf-8").replace("실습이다.", "실습이다. Before/After를 본다. Before/After를 또 본다. 줄을 긋는다.\n\n---\n"), encoding="utf-8")
        # 결함: 병합 뒤 원고를 직접 고쳐도 기록 없이 출판까지 갔다
        self.assertEqual(self.status()["action"], "publish-fix")
        self.log("publish-fix", HEAD + "| 3부 문장 보강 | 🟡 | 편집 | ✅ | |\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main([str(ms), "--report", str(out / "verify-report.md")]), 3)
            # 결함: 기준 점수를 낮춰 다시 돌리면 판정이 '통과'로 바뀌어 낮은 점수 승인을 건너뛰었다
            verify.main([str(ms), "--report", str(out / "verify-report.md"), "--min-score", "0"])
        s = self.status()
        self.assertEqual(s["checkpoint"], "low-score", "9점 미만이면 사람 승인 없이 못 간다")
        self.assertEqual(self.bk("approve", "facts")[0], 1, "낮은 점수 승인보다 사실 확인이 먼저일 수 없다")
        self.assertEqual(self.bk("approve", "low-score")[0], 1, "사유 없이 승인할 수 없다")
        self.ok("approve", "low-score", "--note", "테스트")
        # 결함: 그림을 사실 확인 뒤에 만들어 그림 속 수치가 검사를 거치지 않았다. 이제 그림이 먼저다
        self.assertEqual(self.status()["action"], "figures")
        self.assertEqual(self.bk("approve", "facts")[0], 1, "그림 없이 사실 확인을 승인할 수 없다")
        # 결함: 사실 확인 표가 'fk.' 줄의 앞 80자만 보여 여러 줄 호출과 matplotlib 직접 그림의 수치가 빠졌다
        (out / "figures.py").write_text("".join(f'fk.bars({n}, ["가", "나"],\n        [{n}0, 300])\n' for n in range(1, 5))
                                        + 'import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\n'
                                          'ax.bar(["가", "나"], [73.5, 26.5])\nax.set_title("박지훈 팀장 제공")\nfk.save(fig, 5)\n', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            figure_kit.run(out / "figures.py")
        self.assertEqual(self.status()["checkpoint"], "facts")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_docx.main([str(ms), "--out", str(out / "final.docx")]), 1, "사실 확인 승인 전에는 변환하지 않는다")
            # 결함: 작업 폴더 밖(다운로드 폴더 등)으로 변환하면 승인 확인이 꺼졌다
            elsewhere = pathlib.Path(tempfile.mkdtemp()) / "final.docx"
            self.assertEqual(build_docx.main([str(ms), "--out", str(elsewhere)]), 1, "출력 위치와 무관하게 같은 게이트를 거친다")
        # 결함: 사실 확인 표를 만들지 않고도 사실 확인 승인이 됐다
        self.assertEqual(self.bk("approve", "facts")[0], 1, "표를 먼저 만들어 보여 준다")
        self.ok("facts")
        fc = (out / "fact-check.md").read_text(encoding="utf-8")
        self.assertIn("출처에 기록된 내용", fc)
        self.assertIn("⚠️ 10, ⚠️ 300", fc, "그림 속 수치가 나오고, 출처 기록에 없는 수치는 ⚠️")
        self.assertIn("73.5", fc, "직접 그린 그림의 값도 나온다")
        self.assertIn("④ 실명일 수 있는 표현", fc)
        self.assertIn("| 박지훈 팀장 | 실명 후보 | 그림 5", fc, "그림 속 실명도 사람이 본다")
        self.ok("approve", "facts")
        self.assertEqual(self.status()["action"], "build")
        # 결함: 사실 확인 승인 뒤 'AI 제작 고지'를 바꾸고 accept-toc만 하면 재승인 없이 변환됐다
        toc = pathlib.Path("user_input/user-book-toc.md")
        orig = toc.read_text(encoding="utf-8")
        toc.write_text(orig.replace("- AI 제작 고지: 이 책은 AI 도구의 도움을 받아 쓰고 저자가 검수했다", "- AI 제작 고지: 없음"), encoding="utf-8")
        self.ok("accept-toc")
        with contextlib.redirect_stdout(io.StringIO()):
            verify.main([str(ms), "--report", str(out / "verify-report.md")])
        self.assertIn(self.status()["checkpoint"], ("low-score", "facts"), "설정이 바뀌면 출판 승인을 다시 받는다")
        toc.write_text(orig, encoding="utf-8")
        self.ok("accept-toc")
        with contextlib.redirect_stdout(io.StringIO()):
            verify.main([str(ms), "--report", str(out / "verify-report.md")])
        self.ok("approve", "facts")
        # 결함: 사실 확인 승인 뒤 01_citations.json(출처 제목·링크)을 고쳐도 재승인 없이 출판됐다
        cpath = self.d / "01_citations.json"
        corig = cpath.read_text(encoding="utf-8")
        cpath.write_text(corig.replace("https://example.com/official", "https://example.com/changed"), encoding="utf-8")
        self.assertEqual(self.status()["checkpoint"], "facts", "출처 파일이 바뀌면 사실 확인을 다시 받는다")
        cpath.write_text(corig, encoding="utf-8")
        # 결함: 변환할 때 --toc로 다른 설정을 끼우면 승인과 다른 고지·규칙이 반영됐다
        alt = pathlib.Path("alt.md")
        alt.write_text(orig, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_docx.main([str(ms), "--out", str(out / "final.docx"), "--toc", str(alt)]), 1)
        # 결함: 승인 뒤 그림 데이터를 바꿔도 재승인 없이 출판됐다
        fpy = out / "figures.py"
        forig = fpy.read_text(encoding="utf-8")
        fpy.write_text(forig.replace("[10, 300]", "[99, 300]"), encoding="utf-8")
        self.assertEqual(self.status()["checkpoint"], "facts", "그림 데이터가 바뀌면 사실 확인을 다시 받는다")
        fpy.write_text(forig, encoding="utf-8")
        # 결함: 다른 이름의 링크로 초안을 15_manuscript.md처럼 위장할 수 있었다
        fake = pathlib.Path(tempfile.mkdtemp()) / "15_manuscript.md"
        fake.symlink_to((self.d / "03_draft-v1.md").resolve())
        self.assertIsNotNone(build_docx.gate_reason(fake, out / "final.docx"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_docx.main([str(ms), "--out", str(out / "final.docx")]), 0)
        self.assertEqual(self.status()["action"], "metadata")
        (out / "metadata.md").write_text("# 메타데이터", encoding="utf-8")
        s = self.status()
        self.assertEqual((s["action"], s["done"]), ("done", book.TOTAL_STEPS))
        # 출판 뒤 원고를 고치면 사실 확인과 변환을 다시 해야 한다
        ms.write_text(ms.read_text(encoding="utf-8").replace("실습이다.", "실습이다. 덧붙임."), encoding="utf-8")
        self.assertEqual(self.status()["action"], "publish-fix", "승인 뒤 다시 고치면 수정 기록 새 행부터 요구한다")
        self.assertTrue(any(e.get("edited_after_merge") for e in self.state()["log"] if e["event"] == "approve"), "병합 뒤 수정량이 기록된다")


if __name__ == "__main__":
    unittest.main()
