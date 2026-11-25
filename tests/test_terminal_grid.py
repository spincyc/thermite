"""The terminal remote's grid (tui/remote/grid.js) and painter (paint.js)
against a small model of a terminal (tests/terminal_grid_harness.js).

Random frames from a seeded PRNG are painted over one another, as the
remote paints, and played on a terminal that writes each character at its
cursor and moves it the columns glibc's wcwidth gives the character (a
table of its own, not grid.js's classes): every write must land
in the grid's cell for it, and every frame show what the grid holds.
Nothing leaves the process.

And what says a state where colour is short (tui/remote/styles.js LIGHTS,
theme.js, paint.js sgr): each light's glyph, the focus without colour, and
the states' colours in xterm's 256, for every palette thermite ships.
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

    def test_a_cell_a_terminal_draws_wider_keeps_the_rest_of_the_row_in_place(self):
        # kitty draws the heart with VS16 two columns wide: the space after clears it, and a and b keep their columns.
        self.assertEqual(self.proof["wider"], ["  ab", []])

    def test_no_two_states_of_a_light_share_a_glyph_and_each_takes_one_column(self):
        lights = self.proof["lights"]
        self.assertEqual(sorted(lights["glyphs"]), ["bad", "busy", "off", "ok", "partial", "stale", "unconfirmed"])
        glyphs = [glyph for state in lights["glyphs"].values() for glyph in state]
        self.assertEqual(len(set(glyphs)), len(glyphs), lights["glyphs"])
        self.assertEqual(set(lights["columns"]), {1})
        # A state the table lacks draws as off; only switching has a second glyph, for where motion is on.
        self.assertEqual(lights["drawn"][-1], [lights["glyphs"]["off"][0], "led.off", lights["glyphs"]["off"][0]])
        self.assertEqual([entry[1] for entry in lights["drawn"][:-1]], ["led." + name for name in lights["glyphs"]])
        self.assertEqual([name for name, state in lights["glyphs"].items() if len(state) > 1], ["busy"])

    def test_without_colour_every_state_is_told_apart_and_the_focus_is_reverse_video(self):
        lights = self.proof["lights"]
        self.assertEqual(sorted(lights["palettes"]), ["contrast dark", "contrast light", "thermite dark", "thermite light"])
        for name, palette in lights["palettes"].items():
            with self.subTest(palette=name):
                # What a cell shows without colour is its glyph and its attributes: no two states the same.
                shown = [(glyph, palette["mono"][state]) for state, glyphs in lights["glyphs"].items() for glyph in glyphs]
                self.assertEqual(len(set(shown)), len(shown))
                self.assertEqual(palette["mono"]["bad"], "[0;1m", "a problem is bold as well")
                self.assertEqual(palette["unseen"], [], "focus styles that are not reverse video without colour")

    def test_in_256_colours_on_switching_and_a_problem_are_colours_of_their_own(self):
        for name, palette in self.proof["lights"]["palettes"].items():
            with self.subTest(palette=name):
                colors = palette["colors"]
                told = [colors[state] for state in ("ok", "busy", "bad", "hotkey")]
                self.assertEqual(len(set(told)), 4, colors)
                self.assertEqual(colors["alert"], colors["bad"])
                self.assertEqual(colors["unconfirmed"], colors["bad"])
                # A state's colour is never one of the grays; a light that is off or stale has none.
                self.assertEqual(sorted(palette["grays"]), ["off", "stale"])
                self.assertEqual(colors["stale"], colors["off"])

    def test_blit_preserves_multi_row_hits_and_ignores_null_hits(self):
        blit = self.proof["blitHits"]
        self.assertEqual(blit["srcCount"], [1, 1], "null hit must not be stored")
        self.assertEqual(blit["full"], [None, "card", "card", "card", None])
        self.assertEqual(blit["topClip"], ["card", "card", None])
        self.assertEqual(blit["bottomClip"], [None, "card", None])

    def test_random_frames_show_on_a_terminal_as_the_grid_holds_them(self):
        for run in self.proof["fuzz"]:
            self.assertEqual(run, {"diverged": 0, "faults": []})

    def test_ascii_fast_path_and_blit_reuse_preserve_isolation_and_wide_mending(self):
        script = r"""
const assert = require('assert/strict');
const { Grid, length } = require('./tui/remote/grid');
assert.equal(length('Hello, world! 123'), 17);
assert.equal(length('a\tb'), 3);
const src = new Grid(8, 2, 'base');
src.put(0, 0, 'ABCD', 'strong');
src.put(0, 1, '東京xy', 'base');
const dst = new Grid(8, 2, 'base');
dst.blit(src, 0, 0, 0, 8, 2);
// Mutating dst with ASCII must not alias/corrupt src cells.
dst.put(0, 0, 'ZZZZ', 'dim');
assert.equal(src.text().split('\n')[0], 'ABCD');
assert.equal(dst.text().split('\n')[0], 'ZZZZ');
// Overwriting the left half of a wide character with ASCII mends the right half to a space.
dst.put(0, 1, 'a', 'base');
assert.equal(dst.rows[1][0].ch, 'a');
assert.equal(dst.rows[1][1].ch, ' ');
assert.equal(dst.rows[1][2].ch, '京');
assert.equal(src.rows[1][0].ch, '東');
"""
        run = subprocess.run([shutil.which("node"), "-e", script], cwd=AREA, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
