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

    def test_a_mono_character_stands_in_for_a_narrow_cell_only(self):
        self.assertEqual(self.proof["mono"], [None, None, "#", None])

    def test_the_painter_places_what_follows_a_right_half_itself(self):
        self.assertEqual(self.proof["orphan"], [" a東b", []])

    def test_random_frames_show_on_a_terminal_as_the_grid_holds_them(self):
        for run in self.proof["fuzz"]:
            self.assertEqual(run, {"diverged": 0, "faults": []})


if __name__ == "__main__":
    unittest.main()
