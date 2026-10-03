"""판정이 갈렸던 실제 문장 모음(fixtures/corpus.tsv)으로 정규식 회귀를 막는다."""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "book-publishing" / "scripts"))
import _common as C  # noqa: E402
import verify  # noqa: E402

CHECKS = {
    "name": lambda s: bool(verify.name_candidates(s, set())),
    "num": lambda s: bool(verify.NUMERIC_CLAIM_RE.search(s)) and not verify.RHETORIC_RE.search(s),
    "first": lambda s: bool(verify.FIRST_PERSON_RE.search(s)),
    "soft": lambda s: bool(verify.FIRST_PERSON_SOFT_RE.search(s)),
    "chapter": lambda s: bool(C.CHAPTER_LABEL_RE.match(s)),
    # 금지 용어 매칭: '용어 || 문장'. 표기 변형은 잡고, 다른 낱말 안에 든 경우는 뺀다
    "leak": lambda s: bool(verify.loose(s.split(" || ")[0]).search(s.split(" || ")[1])),
}


class TestCorpus(unittest.TestCase):
    def test_corpus(self):
        rows = [ln.split("\t") for ln in (HERE / "fixtures" / "corpus.tsv").read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        self.assertGreater(len(rows), 60)
        wrong = [f"{cat} 기대 {exp}: {text}" for cat, exp, text in rows if CHECKS[cat](text) != (exp == "1")]
        self.assertEqual(wrong, [], "\n".join(wrong))


if __name__ == "__main__":
    unittest.main()
