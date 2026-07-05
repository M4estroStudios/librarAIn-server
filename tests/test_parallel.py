from __future__ import annotations

import unittest

from src.core.parallel import parallel_map


class TestParallelMap(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(parallel_map(lambda value: value * 2, []), [])

    def test_single_item(self) -> None:
        self.assertEqual(parallel_map(lambda value: value + 1, [1]), [2])

    def test_preserves_order(self) -> None:
        values = list(range(20))
        doubled = parallel_map(lambda value: value * 2, values)
        self.assertEqual(doubled, [value * 2 for value in values])


if __name__ == "__main__":
    unittest.main()
