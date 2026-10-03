"""build_docx.py 테스트. python-docx가 없으면 건너뛴다."""
import contextlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "book-publishing" / "scripts"))
import build_docx  # noqa: E402

FIX = HERE / "fixtures"


@unittest.skipIf(build_docx.Document is None, "python-docx 없음")
class TestBuild(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        for f in ("15_manuscript.md", "01_citations.json", "toc.md"):
            shutil.copy(FIX / f, self.d / f)
        (self.d / "images").mkdir()
        try:
            from PIL import Image
            for n in range(1, 6):
                Image.new("RGB", (40, 20), "white").save(self.d / "images" / f"fig{n:02d}.png")
        except ImportError:
            self.skipTest("Pillow 없음")

    def tearDown(self):
        shutil.rmtree(self.d)

    def build(self, *extra, gate=False):
        args = [str(self.d / "15_manuscript.md"), "--out", str(self.d / "final.docx"), "--toc", str(self.d / "toc.md"), *extra]
        if not gate:
            args.append("--no-gate")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return build_docx.main(args)

    def text(self):
        doc = build_docx.Document(str(self.d / "final.docx"))
        return "\n".join(p.text for p in doc.paragraphs)

    def test_gate_refuses_manuscripts_outside_a_book_folder(self):
        # 게이트는 책 폴더의 상태로만 판정한다. 상태가 없는 원고는 검사 보고서가 통과여도 변환하지 않는다
        import verify
        with contextlib.redirect_stdout(io.StringIO()):
            verify.main([str(self.d / "15_manuscript.md"), "--toc", str(self.d / "toc.md"), "--report", str(self.d / "verify-report.md")])
        self.assertEqual(self.build(gate=True), 1)
        self.assertIn("--no-gate", build_docx.gate_reason(self.d / "15_manuscript.md", self.d / "final.docx"))

    def test_heading_styles_have_no_theme_fonts(self):
        # 결함: 기본 템플릿의 테마 글꼴 속성 때문에 제목이 다른 글꼴(Carlito 등)로 바뀌었다
        self.build()
        styles = build_docx.Document(str(self.d / "final.docx")).styles.element.xml
        self.assertNotIn("asciiTheme", styles)
        self.assertNotIn("eastAsiaTheme", styles)

    @unittest.skipUnless(__import__("shutil").which("soffice") and __import__("shutil").which("pdftotext"), "LibreOffice, pdftotext 없음")
    def test_pdf_toc_page_numbers_match_real_pages(self):
        # 결함: PDF 차례에 쪽 번호가 없었고, 이후에는 본문 속 '부록의 …' 언급에 걸려 한 쪽 어긋났다
        md = self.d / "15_manuscript.md"
        md.write_text(md.read_text(encoding="utf-8").replace("- 출처를 확인한다", "- 출처를 확인한다\n\n부록의 체크리스트를 쓴다."), encoding="utf-8")
        self.assertEqual(self.build("--pdf"), 0)
        import re
        import subprocess
        pages = subprocess.run(["pdftotext", "-layout", str(self.d / "final.pdf"), "-"], capture_output=True, text=True).stdout.split("\f")
        self.assertIn("차례", pages[1], "표지 다음 쪽이 차례다 (빈 쪽 없음)")
        toc = dict((t.strip(), int(n)) for t, n in re.findall(r"^\s*(\S[^\n.]*?)\.{3,}\s*(\d+)\s*$", pages[1], re.M))
        self.assertGreaterEqual(len(toc), 5, pages[1])
        for title, page in toc.items():
            body = [ln.strip() for ln in pages[page - 1].splitlines() if ln.strip() and not ln.strip().isdigit()]
            self.assertTrue(any(ln.replace(" ", "").startswith(title.replace(" ", "")[:8]) for ln in body[1:4]),
                            f"차례 '{title}' {page}쪽인데 그 쪽 머리에 제목이 없다: {body[:4]}")

    def test_trial_build_is_marked(self):
        # 결함: --no-gate로 만든 시험본이 정상본과 똑같은 표지로 나왔다
        self.assertEqual(self.build(), 0)
        doc = build_docx.Document(str(self.d / "final.docx"))
        cover = "\n".join(p.text for p in doc.paragraphs[:12])
        self.assertIn("시험 변환본", cover)
        header = doc.sections[0].header.paragraphs[0].text
        self.assertIn("시험 변환본", header)

    def test_figure_labels_wrap_inside_boxes(self):
        # 결함: timeline과 cards의 긴 라벨이 줄바꿈 없이 넘쳐 이웃 글자와 겹쳤다
        import figure_kit as fk
        long = "아주 긴 설명이 들어가는 라벨이라서 한 줄에 다 들어가지 않는다"
        self.assertGreater(fk._wrap(long, 2.6, 12).count("\n"), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            fk.setup(out_dir=str(self.d / "figs"))
            fk.timeline(1, [("1주차", long), ("2주차", long)])
            fk.cards(2, [(long, long), ("짧음", "짧음")])
        self.assertTrue((self.d / "figs" / "fig01.png").is_file() and (self.d / "figs" / "fig02.png").is_file())

    def test_stale_pdf_is_removed_and_dates_are_current(self):
        # 결함: --pdf 없이 다시 변환하면 옛 final.pdf가 남아 고친 내용이 빠진 PDF가 배포될 수 있었다
        (self.d / "final.pdf").write_bytes(b"%PDF-old")
        self.assertEqual(self.build(), 0)
        self.assertFalse((self.d / "final.pdf").exists())
        doc = build_docx.Document(str(self.d / "final.docx"))
        self.assertGreaterEqual(doc.core_properties.created.year, 2026, "문서 만든 날짜는 변환한 날")
        self.assertIn("- 결과 해시:", (self.d / "build-report.md").read_text(encoding="utf-8"))

    def test_emphasis_variants_render_cleanly(self):
        # 결함: '**굵게 *기울임* 섞기**'는 ** 잔존으로 변환이 실패했고, '***둘 다***'와 '__굵게__'는 기호가 그대로 찍혔다
        b = build_docx.Builder((self.d / "toc.md").read_text(encoding="utf-8"), str(self.d), "•")
        p = b.doc.add_paragraph()
        b.inline(p, "앞 **굵게 *기울임* 섞기** 중간 ***둘 다*** 끝 __밑줄식 굵게__ 마침.")
        self.assertNotIn("*", p.text)
        self.assertNotIn("__", p.text)
        bold = "".join(r.text for r in p.runs if r.bold)
        self.assertIn("기울임", bold)
        self.assertIn("밑줄식 굵게", bold)
        self.assertTrue(any(r.italic and r.bold and r.text == "둘 다" for r in p.runs))

    def test_distribution_label_and_reproducible_date(self):
        # 결함: 사내 배포 책에 '사내 한정' 같은 배포 등급을 찍을 수 없었고, 문서 날짜가 빌드마다 달라졌다
        toc = (self.d / "toc.md").read_text(encoding="utf-8").replace("## 출판 설정\n", "## 출판 설정\n\n- 배포 표기: 사내 한정\n", 1)
        self.assertIn("배포 표기", toc)
        os.environ["SOURCE_DATE_EPOCH"] = "1767225600"  # 2026-01-01
        try:
            b = build_docx.Builder(toc, str(self.d), "•")
            b.cover()
        finally:
            del os.environ["SOURCE_DATE_EPOCH"]
        self.assertIn("사내 한정", b.doc.sections[0].header.paragraphs[0].text)
        self.assertIn("사내 한정", "\n".join(p.text for p in b.doc.paragraphs))
        self.assertIn("2026년 1월", "\n".join(p.text for p in b.doc.paragraphs))
        self.assertEqual(b.doc.core_properties.created.year, 2026)

    def test_figure_kit_records_text_and_rejects_bad_input(self):
        # 결함: 그림 속 글자와 수치가 어디에도 기록되지 않았고, 빈 입력에서 0으로 나누기 오류가 났다
        import json
        import figure_kit as fk
        with contextlib.redirect_stderr(io.StringIO()):
            fk.setup(out_dir=str(self.d / "figs"))
            fk.bars(1, ["가", "나"], [-5, 300], unit="명")
            fk.cards(2, ["제목만", ("제목", "설명")])
            for bad in (lambda: fk.flow(3, []), lambda: fk.timeline(3, []), lambda: fk.bars(3, ["가"], [1, 2])):
                with self.assertRaises(ValueError):
                    bad()
        rec = json.loads((self.d / "figs" / "figdata.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["fig01"]["values"], [-5, 300])
        self.assertIn("300명", " ".join(rec["fig01"]["texts"]))
        self.assertIn("제목만", rec["fig02"]["texts"])

    def test_final_document_is_scanned_for_leaks(self):
        # 결함: 코드 블록(프롬프트 예시)과 출처 제목의 금지 용어가 검사를 빠져 최종 PDF에 찍혔다. 완성 문서를 마지막으로 다시 본다
        ms = self.d / "15_manuscript.md"
        ms.write_text(ms.read_text(encoding="utf-8").replace("<!-- STAGE_COMPLETE", "```\n커스텀 지시 예시\n```\n\n<!-- STAGE_COMPLETE", 1), encoding="utf-8")
        self.assertEqual(self.build(), 0, "시험 변환은 쪽수를 재는 용도라 경고로만 남긴다")
        self.assertIn("🟡 완성 문서에 새면 안 되는 표현", (self.d / "build-report.md").read_text(encoding="utf-8"))
        toc = (self.d / "toc.md").read_text(encoding="utf-8")
        fails, _ = build_docx.leak_check(self.d / "final.docx", toc, self.d / "toc.md", ms, "필명")
        self.assertTrue(fails and "커스텀 지시" in fails[0], "출판 변환에서는 실패로 센다")

    def test_figure_kit_records_table_cells_and_hash(self):
        # 결함: ax.table 칸의 이름이 기록되지 않았고, 기록이 그림 파일과 묶여 있지 않았다
        import json
        import figure_kit as fk
        import matplotlib.pyplot as plt
        with contextlib.redirect_stderr(io.StringIO()):
            fk.setup(out_dir=str(self.d / "figs"))
            fig, ax = plt.subplots()
            ax.axis("off")
            ax.table(cellText=[["이수진", "강다은"]], loc="center")
            path = fk.save(fig, 4)
        rec = json.loads((self.d / "figs" / "figdata.json").read_text(encoding="utf-8"))["fig04"]
        self.assertIn("강다은", rec["texts"] + rec["cells"])
        self.assertEqual(rec["sha"], build_docx.C.sha(path))

    def test_static_toc_fallback(self):
        # 결함: PDF로 바꾸면 차례 자리에 안내 문구만 남았다
        self.build()
        t = self.text()
        toc_part = t[t.index("차례"):t.index("차례") + 200]
        self.assertIn("제1부: 기초", toc_part)
        self.assertIn("제1장. 첫 번째 장", toc_part)
        self.assertNotIn("1부를 마치며", toc_part, "브릿지는 차례에 넣지 않는다")

    def test_unknown_citation_fails(self):
        md = self.d / "15_manuscript.md"
        md.write_text(md.read_text(encoding="utf-8").replace("cite_001", "cite_404"), encoding="utf-8")
        self.assertEqual(self.build(), 1)
        self.assertIn("cite_404", (self.d / "build-report.md").read_text(encoding="utf-8"))

    def test_example_tag_is_removed(self):
        md = self.d / "15_manuscript.md"
        md.write_text(md.read_text(encoding="utf-8").replace("실습이다.", "응답자는 30명이었다 [[예시]]."), encoding="utf-8")
        self.assertEqual(self.build(), 0)
        self.assertIn("응답자는 30명이었다.", self.text())
        self.assertNotIn("[[예시]]", self.text())

    def test_build_and_post_check(self):
        self.assertEqual(self.build(), 0, (self.d / "build-report.md").read_text(encoding="utf-8"))
        doc = build_docx.Document(str(self.d / "final.docx"))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertEqual(doc.core_properties.author, "테스트 저자")
        self.assertEqual(sum(p.style.name == "Heading 1" for p in doc.paragraphs), 5, "부 4개 + 책 끝 '출처'")
        self.assertNotIn("<!--", text)
        self.assertIn("[1]", text, "출처 태그는 위첨자 번호로 바뀐다")
        self.assertIn("출처", text)
        self.assertIn("그림 4에서", text, "본문 속 [그림 N] 참조는 괄호를 뗀다")
        self.assertIn("AI 도구의 도움", text, "AI 제작 고지가 표지에 들어간다")
        styles = doc.styles.element.xml
        self.assertIn('autoSpaceDE w:val="0"', styles, "한글과 영문 사이 자동 간격을 끈다")

    def test_bridge_is_not_a_chapter_heading(self):
        self.build()
        doc = build_docx.Document(str(self.d / "final.docx"))
        h2 = [p.text for p in doc.paragraphs if p.style.name == "Heading 2"]
        self.assertNotIn("1부를 마치며", h2)

    def test_missing_image_fails_unless_allowed(self):
        (self.d / "images" / "fig03.png").unlink()
        self.assertEqual(self.build(), 1)
        self.assertIn("fig03", (self.d / "build-report.md").read_text(encoding="utf-8"))
        self.assertEqual(self.build("--allow-missing-images"), 0)
        # 허용해도 판정은 '통과'가 아니다 (status가 완료로 보지 않게)
        self.assertIn("- 판정: 시험 변환", (self.d / "build-report.md").read_text(encoding="utf-8"))

    def test_code_block_literals_are_allowed(self):
        # 코드 블록 안의 **는 그대로 실리는 것이 정상이다
        self.assertEqual(self.build(), 0)
        md = (self.d / "15_manuscript.md").read_text(encoding="utf-8")
        (self.d / "15_manuscript.md").write_text(md.replace("**강조**는", "**강조**는 *기울임*과"), encoding="utf-8")
        self.assertEqual(self.build(), 0)


if __name__ == "__main__":
    unittest.main()
