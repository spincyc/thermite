"""The terminal remote's grid (tui/remote/grid.js) and painter (paint.js)
against a small model of a terminal (tests/terminal_grid_harness.js).

Random frames from a seeded PRNG are painted over one another, as the
remote paints, and played on a terminal that writes each character at its
cursor and moves it the columns the character takes: every write must land
in the grid's cell for it, and every frame show what the grid holds.
Nothing leaves the process.
"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class TerminalGrid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/terminal_grid_harness.js")],
                             text=True, capture_output=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.proof = json.loads(run.stdout)

    def test_a_wide_character_is_never_cut_by_the_grids_edges(self):
        # In the last column it has no room for its right half; drawn from before the first, only that half is left.
        self.assertEqual(self.proof["lastColumn"], [" ", " ", " ", "a", " "])
        self.assertEqual(self.proof["firstColumn"], [" ", "京", "", "a", " "])

    def test_a_code_point_takes_the_columns_wcwidth_gives_it(self):
        # Devanagari and Thai: a spacing mark takes a column, a nonspacing one none; a ZWJ sequence
        # two for each emoji, VS16 none (the heart is text by default), a lone regional indicator one.
        self.assertEqual(self.proof["columns"], [9, 4, 6, 1, 1, 4, 1])
        self.assertEqual(self.proof["thai"], ["\u0e01", "\u0e33", "\u0e25\u0e31", "\u0e07", " "])
        # Where glibc's wcwidth parts from a block's: I Ching symbols, Khitan, Kana Extended-B and Tai Xuan Jing
        # take two; a Hangul vowel of Jamo Extended-B, the Hangul fillers none.
        self.assertEqual(self.proof["blocks"], [2, 2, 2, 2, 2, 2, 2, 1, 1])
        # A format character a terminal may draw (a soft hyphen) or act on (a bidi isolate) goes; a flag's tags stay.
        self.assertEqual(self.proof["format"], [[7, "Example"], [5, "ab cd"],
                                                [2, "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"]])
        # A joiner is drawn in the cell before it: the wide character's own, not its half.
        self.assertEqual(self.proof["zwj"], ["\U0001f468\u200d", "", "\U0001f469\u200d", "", "\U0001f467", "", " ", " "])

    def test_a_flag_is_one_wide_character_drawn_in_its_two_columns(self):
        # A terminal as wcwidth draws each indicator in a column; others, the flag in two. A third is its own.
        japan, britain, alone = "\U0001f1ef\U0001f1f5", "\U0001f1ec\U0001f1e7", "\U0001f1e6"
        self.assertEqual(self.proof["flags"], [[japan, "", britain, "", alone, "a", " ", " "],
                                               japan + britain + alone + "a", []])

    def test_a_wide_word_wraps_a_whole_character_to_a_line_and_no_more(self):
        tokyo = ["\u6771", "\u4eac"]
        self.assertEqual(self.proof["wrap"], [tokyo, tokyo, ["ab"] + tokyo + ["\u6771"]])

    def test_a_mono_character_stands_in_for_a_narrow_cell_only(self):
        self.assertEqual(self.proof["mono"], [None, None, "#", None])

    def test_the_painter_places_what_follows_a_right_half_itself(self):
        self.assertEqual(self.proof["orphan"], [" a東b", []])

    def test_random_frames_show_on_a_terminal_as_the_grid_holds_them(self):
        for run in self.proof["fuzz"]:
            self.assertEqual(run, {"diverged": 0, "faults": []})


if __name__ == "__main__":
    unittest.main()
