#!/usr/bin/env python3
"""The control-flow checker must find what it claims to find (and nothing else)."""
import unittest

from check_control_flow import scan


def messages(source):
    return [message.split(" ")[0] + " " + message.split(" ")[1] for _, message in scan(source)]


class CheckerTests(unittest.TestCase):
    def test_unmarked_except_and_raise_are_findings(self):
        source = "try:\n    x()\nexcept ValueError:\n    raise RuntimeError('x')\n"
        self.assertEqual(len(scan(source)), 2)

    def test_marker_on_same_line_or_comment_line_above(self):
        same = "try:\n    x()\nexcept ValueError:  # exception-ok: parsing untrusted input\n    pass\n"
        above = "try:\n    x()\n# exception-ok: network boundary\nexcept OSError:\n    pass\n"
        self.assertEqual(scan(same), [])
        self.assertEqual(scan(above), [])

    def test_marker_needs_a_reason_and_must_be_adjacent(self):
        empty = "try:\n    x()\nexcept ValueError:  # exception-ok:\n    pass\n"
        distant = "# exception-ok: too far away\n\ntry:\n    x()\nexcept ValueError:\n    pass\n"
        code_above = "y = 1  # exception-ok: belongs to this line\nraise ValueError()\n"
        for source in (empty, distant, code_above):
            with self.subTest(source=source):
                self.assertEqual(len(scan(source)), 1)

    def test_error_text_parsing_is_a_finding_even_when_marked(self):
        compared = "try:\n    x()\nexcept OSError as e:  # exception-ok: network\n    if 'refused' in str(e):\n        pass\n"
        searched = "try:\n    x()\nexcept OSError as e:  # exception-ok: network\n    ok = str(e).lower().startswith('the model')\n"
        args = "try:\n    x()\nexcept OSError as e:  # exception-ok: network\n    if e.args[0] == 1:\n        pass\n"
        for source in (compared, searched, args):
            with self.subTest(source=source):
                self.assertEqual(len(scan(source)), 1)

    def test_logging_error_text_is_not_parsing(self):
        source = "try:\n    x()\nexcept OSError as e:  # exception-ok: network\n    log.warning('failed: %s', e)\n    note = str(e)\n"
        self.assertEqual(scan(source), [])

    def test_values_are_no_findings(self):
        source = "def f(x):\n    if not isinstance(x, str):\n        return invalid('x must be text')\n    return x\n"
        self.assertEqual(scan(source), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
