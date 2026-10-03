"""문서와 코드가 서로 어긋나지 않는지 본다.

평가에서 반복해 나온 결함 유형: 문서가 없는 명령을 지시함, 템플릿 기본값과 코드 기본값이 다름,
README 숫자가 실제와 다름, 단계 표가 가리키는 문서 헤딩이 없음.
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import _common as C  # noqa: E402
import book  # noqa: E402
import build_docx  # noqa: E402
import verify  # noqa: E402

SKILL = ROOT  # 저장소 루트가 곧 스킬 폴더다 (SKILL.md, references/, scripts/, user_input/, assets/)
DOCS = [SKILL / "SKILL.md", ROOT / "README.md"] + sorted((SKILL / "references").glob("*.md"))
TEMPLATE = (SKILL / "user_input" / "user-book-toc.md").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "example" / "user-book-toc.md").read_text(encoding="utf-8")


def subcommands():
    ap_src = (SKILL / "scripts" / "book.py").read_text(encoding="utf-8")
    return set(re.findall(r'sub\.add_parser\("([\w-]+)"\)', ap_src))


class TestConsistency(unittest.TestCase):
    def test_template_rule_defaults_match_code(self):
        vals, unknown = C.parse_kv(C.get_section(TEMPLATE, "검증 규칙"), verify.RULE_DEFAULTS.keys())
        self.assertEqual(unknown, [])
        for k, v in verify.RULE_DEFAULTS.items():
            self.assertEqual(vals.get(k, ""), v, f"검증 규칙 '{k}': 템플릿과 코드 기본값이 다르다")

    def test_template_publish_defaults_match_code(self):
        vals, unknown = C.parse_kv(C.get_section(TEMPLATE, "출판 설정"), build_docx.PUB_DEFAULTS.keys())
        self.assertEqual(unknown, [])
        for k, v in build_docx.PUB_DEFAULTS.items():
            self.assertEqual(vals.get(k, ""), v, f"출판 설정 '{k}': 템플릿과 코드 기본값이 다르다")

    def test_example_config_has_no_unknown_keys(self):
        for sec, keys in (("검증 규칙", verify.RULE_DEFAULTS.keys()), ("출판 설정", build_docx.PUB_DEFAULTS.keys())):
            self.assertEqual(C.parse_kv(C.get_section(EXAMPLE, sec), keys)[1], [], sec)
        self.assertFalse(C.PLACEHOLDER_RE.search(EXAMPLE), "예시에는 빈칸이 없어야 한다")
        self.assertGreater(len(C.toc_structure(EXAMPLE)), 5)

    def test_docs_only_mention_existing_commands(self):
        cmds = subcommands()
        for doc in DOCS:
            text = doc.read_text(encoding="utf-8")
            for m in re.finditer(r"(?:BOOK|book\.py)\s+([a-z][\w-]*)", text):
                self.assertIn(m.group(1), cmds, f"{doc.name}: 없는 명령 '{m.group(1)}'")
            for m in re.finditer(r"approve\s+([a-z][\w-]*)", text):
                self.assertIn(m.group(1), book.APPROVAL_TARGET, f"{doc.name}: 없는 승인 대상 '{m.group(1)}'")

    def test_docs_only_mention_existing_script_options(self):
        opts = {
            "verify.py": set(re.findall(r'"(--[\w-]+)"', (SKILL / "scripts" / "verify.py").read_text(encoding="utf-8"))),
            "build_docx.py": set(re.findall(r'"(--[\w-]+)"', (SKILL / "scripts" / "build_docx.py").read_text(encoding="utf-8"))),
        }
        for doc in DOCS:
            for line in doc.read_text(encoding="utf-8").splitlines():
                for script, known in opts.items():
                    if script in line:
                        for o in re.findall(r"(?<![\w-])(--[a-z][\w-]*)", line):
                            self.assertIn(o, known, f"{doc.name}: {script}에 없는 옵션 {o}")

    def test_stage_docs_point_to_existing_headings(self):
        for st in book.STAGES:
            path, _, section = st["doc"].partition(" ")
            text = (SKILL / path).read_text(encoding="utf-8")
            if section:
                heads = [t for _, _, t in C.iter_headings(text)]
                self.assertTrue(any(section in h for h in heads), f"{st['id']}: {path}에 '{section}' 헤딩이 없다")

    def test_progress_total_in_docs(self):
        for doc in DOCS:
            for m in re.finditer(r"\d+/(\d+)\]", doc.read_text(encoding="utf-8")):
                self.assertEqual(int(m.group(1)), book.TOTAL_STEPS, f"{doc.name}: 전체 단계 수 {m.group(1)}")

    def test_readme_test_count(self):
        loader = unittest.TestLoader()
        n = loader.discover(str(ROOT / "tests")).countTestCases()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for m in re.finditer(r"(?:Tests-|테스트 )(\d+)", readme):
            self.assertEqual(int(m.group(1)), n, "README의 테스트 수가 실제와 다르다")

    def test_skill_frontmatter_and_size(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = re.match(r"^---\nname: ([^\n]+)\ndescription: ([^\n]+)\n(?:[a-z-]+: [^\n]+\n)*---\n", text)
        self.assertTrue(fm, "SKILL.md frontmatter")
        self.assertLess(len(fm.group(2)), 1024, "description은 1024자 미만")
        self.assertRegex(fm.group(1), r"^[a-z0-9]+(-[a-z0-9]+)*$", "스킬 name은 소문자, 숫자, 하이픈만 (Claude Code 규격)")
        self.assertLess(len(text.splitlines()), 500)

    def test_skill_meets_both_platform_rules(self):
        # Claude 앱과 ChatGPT 업로드 규칙: name은 소문자·숫자·하이픈 64자 이내, 예약어 금지. description은 1024자 이내, XML 태그 금지
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = text.split("---")[1]
        name = __import__("re").search(r"^name: (.+)$", fm, __import__("re").M).group(1).strip()
        desc = __import__("re").search(r"^description: (.+)$", fm, __import__("re").M).group(1).strip().strip('"')
        self.assertEqual(name, "book-publishing")
        self.assertLessEqual(len(name), 64)
        self.assertNotRegex(name, r"anthropic|claude")
        self.assertLessEqual(len(desc), 1024)
        self.assertNotRegex(desc, r"<[^>]+>")
        self.assertRegex(fm, r"(?m)^license: ")
        self.assertTrue((SKILL / "LICENSE").is_file(), "스킬 폴더(저장소 루트)에 LICENSE가 있어야 한다")

    def test_docs_have_no_em_dash(self):
        # 작업 규칙: 줄표 대신 쉼표, 마침표, 괄호를 쓴다 (부호 자체를 설명하는 줄은 예외)
        for doc in DOCS + [SKILL / "user_input" / "user-book-toc.md"]:
            for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"[—–]", line) and "em dash" not in line:
                    self.fail(f"{doc.name}:{i} 줄표 사용")


if __name__ == "__main__":
    unittest.main()
