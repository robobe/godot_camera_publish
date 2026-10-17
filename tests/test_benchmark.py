import unittest

from tools.benchmark_zmq import choose_buffer


def result(size, rates):
    return {'buffer_bytes': size, **{f'viewer{i}_fps': {'mean': rate}
                                     for i, rate in enumerate(rates, 1)}}


class SelectionTest(unittest.TestCase):
    def test_smallest_buffer_meeting_every_viewer_target(self):
        results = [result(65536, [30, 30, 28, 30]), result(262144, [29, 30, 30, 30]),
                   result(1048576, [30]*4)]
        self.assertEqual(choose_buffer(results), 262144)

    def test_fallback_uses_worst_viewer_then_smaller_buffer(self):
        results = [result(65536, [28, 28, 28, 28]), result(262144, [28.04, 29, 29, 29]),
                   result(1048576, [30, 30, 30, 25])]
        self.assertEqual(choose_buffer(results), 65536)
        results[1]['viewer1_fps']['mean'] = 28.2
        self.assertEqual(choose_buffer(results), 262144)


if __name__ == '__main__':
    unittest.main()
