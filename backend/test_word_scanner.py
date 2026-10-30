import unittest

from .word_scanner import is_acceptable


class WordScannerTests(unittest.TestCase):
    def test_clean_strings_pass(self):
        for s in ["Player 42", "good morning", "abc", "a" * 12]:
            self.assertTrue(is_acceptable(s), s)

    def test_length_limits(self):
        self.assertFalse(is_acceptable("ab"))
        self.assertFalse(is_acceptable(""))
        self.assertFalse(is_acceptable("a" * 13))
        self.assertFalse(is_acceptable("a" * 25))

    def test_disallowed_characters(self):
        for s in ["caf\u00e9 time", "tab\there", "new\nline", "emoji \U0001F600 ok"]:
            self.assertFalse(is_acceptable(s), repr(s))

    def test_non_string(self):
        self.assertFalse(is_acceptable(None))
        self.assertFalse(is_acceptable(123))

    def test_bad_words_case_and_whitespace(self):
        # "sandnigger" and "2 girls 1 cup" are on the list.
        for s in [
            "sandnigger",
            "SandNigger",
            "S A N D N I G G E R",
            "sand  nigger",
            "xx 2girls1cup xx",
            "2 GIRLS 1 CUP",
            "my n i g g e r",
        ]:
            self.assertFalse(is_acceptable(s), s)

    def test_separators(self):
        for s in [
            "s.a.n.d.n.i.g.g.e.r",
            "s_a_n_d_n_i_g_g_e_r",
            "n-i-g-g-e-r",
            "n . i _ g - g * e / r",
        ]:
            self.assertFalse(is_acceptable(s), s)

    def test_leetspeak(self):
        for s in [
            "s4ndn1gg3r",
            "$@ND N!GG3R",
            "n1gg3r",
            "n|gger",
            "5.4.n.d n 1 g g 3 r",
        ]:
            self.assertFalse(is_acceptable(s), s)

    def test_leetspeak_does_not_flag_clean_text(self):
        for s in ["Player 42", "good morning", "r0b0t 42", "c00l g4mer"]:
            self.assertTrue(is_acceptable(s), s)


if __name__ == "__main__":
    unittest.main()
