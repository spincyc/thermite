"""The app icon's rasterizer (oh_brand.png) skips outlines a line misses;
its pixels stay those of sampling every outline on every line."""
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_brand as brand  # noqa: E402


def reference(palette, mode: str, size: int, samples: int) -> list[bytes]:
    """The icon's RGB rows, every sample tested against every outline."""
    colors = brand.mark_colors(palette, mode)
    left, top, side = (float(value) for value in brand.TILE)
    count, total = size * samples, samples * samples
    step = side / count
    shapes = [(brand.rgb(colors[role]), shape) for role, path in brand.TORCH
              for shape in brand.stroke_shapes(path, brand.STROKE_WIDTHS[role])]
    rows = []
    for row in range(size):
        sums = [0] * (size * 3)
        for sub in range(samples):
            y = top + (row * samples + sub + 0.5) * step
            line = [brand.rgb(colors["surface"])] * count
            for color, points in shapes:
                for first, end in brand._spans(points, y, left, step, count):
                    line[first:end] = [color] * (end - first)
            for x, pixel in enumerate(line):
                for k in range(3):
                    sums[x // samples * 3 + k] += pixel[k]
        rows.append(bytes((value + total // 2) // total for value in sums))
    return rows


class Icon(unittest.TestCase):
    def test_pixels_match_sampling_every_outline(self):
        for name in brand.builtin():
            for mode in brand.MODES:
                with self.subTest(theme=name, mode=mode):
                    palette = brand.load(name)
                    self.assertEqual(brand.png(palette, mode, 40, 3), brand._png(reference(palette, mode, 40, 3), 40, 40))


if __name__ == "__main__":
    unittest.main()
