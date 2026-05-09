"""
test_logic.py — Unit tests for Long Path Begone

Covers three areas where bugs could cause data loss or silent misbehavior:
  1. to_extended / from_extended  — path prefix manipulation
  2. normalise_replacement        — backreference syntax conversion
  3. apply_replacement            — find/replace logic (regex and literal modes)
  4. rename_segment_walk          — the parents-first segment walk

All tests call the real module functions directly, so a regression in the app
will be caught here without any manual syncing of test helpers.

Run with:  python test_logic.py
       or: python -m pytest test_logic.py -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import long_path_begone as lpb


# =============================================================================
# 1. Path prefix helpers
# =============================================================================

class TestToExtended(unittest.TestCase):

    def test_regular_path_gets_prefix(self):
        result = lpb.to_extended(r"C:\Foo\Bar")
        self.assertTrue(result.startswith(lpb.EXT_PREFIX))

    def test_already_extended_is_unchanged(self):
        p = lpb.EXT_PREFIX + r"C:\Foo\Bar"
        self.assertEqual(lpb.to_extended(p), p)

    def test_empty_string_passthrough(self):
        self.assertEqual(lpb.to_extended(""), "")

    def test_unc_path_gets_unc_prefix(self):
        result = lpb.to_extended(r"\\server\share\file.txt")
        self.assertTrue(result.startswith(lpb.EXT_UNC_PREFIX))

    def test_unc_path_no_double_separator(self):
        result = lpb.to_extended(r"\\server\share\file.txt")
        after_prefix = result[len(lpb.EXT_UNC_PREFIX):]
        self.assertFalse(after_prefix.startswith("\\"),
                         f"unexpected leading separator: {result!r}")

    def test_absolute_round_trip(self):
        original = r"C:\Users\test\deeply\nested\file.txt"
        self.assertEqual(lpb.from_extended(lpb.to_extended(original)), original)


class TestFromExtended(unittest.TestCase):

    def test_strips_ext_prefix(self):
        self.assertEqual(lpb.from_extended(lpb.EXT_PREFIX + r"C:\foo"), r"C:\foo")

    def test_strips_unc_prefix(self):
        result = lpb.from_extended(lpb.EXT_UNC_PREFIX + r"server\share")
        self.assertEqual(result, r"\\server\share")

    def test_plain_path_passthrough(self):
        p = r"C:\foo\bar"
        self.assertEqual(lpb.from_extended(p), p)

    def test_empty_string_passthrough(self):
        self.assertEqual(lpb.from_extended(""), "")


# =============================================================================
# 2. normalise_replacement — backreference syntax
# =============================================================================

class TestNormaliseReplacement(unittest.TestCase):

    def test_dollar_digit(self):
        self.assertEqual(lpb.normalise_replacement("$1"), r"\1")

    def test_dollar_brace_digit(self):
        self.assertEqual(lpb.normalise_replacement("${1}"), r"\g<1>")

    def test_dollar_brace_name(self):
        self.assertEqual(lpb.normalise_replacement("${name}"), r"\g<name>")

    def test_mixed_dollar_and_brace(self):
        self.assertEqual(lpb.normalise_replacement("$1_${2}"), r"\1_\g<2>")

    def test_named_group_brace(self):
        self.assertEqual(lpb.normalise_replacement("${word}_suffix"), r"\g<word>_suffix")

    def test_no_backrefs_unchanged(self):
        self.assertEqual(lpb.normalise_replacement("hello"), "hello")

    def test_trailing_dollar_without_digit_unchanged(self):
        self.assertEqual(lpb.normalise_replacement("price_$"), "price_$")


# =============================================================================
# 3. apply_replacement — find/replace per-path logic
# =============================================================================

class TestApplyReplacement(unittest.TestCase):

    # --- regex mode ---

    def test_simple_regex_replaces_all(self):
        result, changed = lpb.apply_replacement("foo", "bar", "foo and foo",
                                                regex=True, case=True)
        self.assertEqual(result, "bar and bar")
        self.assertTrue(changed)

    def test_backref_dollar_syntax(self):
        result, _ = lpb.apply_replacement(r"(foo)", "$1bar", "foo",
                                          regex=True, case=True)
        self.assertEqual(result, "foobar")

    def test_backref_brace_digit_syntax(self):
        result, _ = lpb.apply_replacement(r"(foo)", "${1}bar", "foo",
                                          regex=True, case=True)
        self.assertEqual(result, "foobar")

    def test_named_group_replacement(self):
        result, _ = lpb.apply_replacement(r"(?P<word>\w+)", "${word}_x", "hello",
                                          regex=True, case=True)
        self.assertEqual(result, "hello_x")

    def test_regex_case_insensitive_matches_all_variants(self):
        result, _ = lpb.apply_replacement("FOO", "bar", "foo Foo FOO",
                                          regex=True, case=False)
        self.assertEqual(result, "bar bar bar")

    def test_regex_case_sensitive_no_match(self):
        result, changed = lpb.apply_replacement("FOO", "bar", "foo",
                                                regex=True, case=True)
        self.assertEqual(result, "foo")
        self.assertFalse(changed)

    def test_regex_no_match_returns_original_unchanged(self):
        result, changed = lpb.apply_replacement("zzz", "X", "hello",
                                                regex=True, case=True)
        self.assertEqual(result, "hello")
        self.assertFalse(changed)

    def test_invalid_regex_raises(self):
        with self.assertRaises(re.error):
            lpb.apply_replacement("[invalid", "X", "hello", regex=True, case=True)

    # --- literal mode ---

    def test_literal_case_sensitive_replaces_exact(self):
        result, changed = lpb.apply_replacement("abc", "XYZ", "ABC abc",
                                                regex=False, case=True)
        self.assertEqual(result, "ABC XYZ")
        self.assertTrue(changed)

    def test_literal_case_insensitive_replaces_all_variants(self):
        result, _ = lpb.apply_replacement("abc", "XYZ", "ABC abc Abc",
                                          regex=False, case=False)
        self.assertEqual(result, "XYZ XYZ XYZ")

    def test_literal_no_match_returns_original_unchanged(self):
        result, changed = lpb.apply_replacement("zzz", "X", "hello",
                                                regex=False, case=True)
        self.assertEqual(result, "hello")
        self.assertFalse(changed)

    # --- backslash safety: Windows paths must survive as-is in the replacement ---

    def test_windows_path_in_replacement_case_sensitive(self):
        result, _ = lpb.apply_replacement("OLD", r"C:\new\folder", "OLD",
                                          regex=False, case=True)
        self.assertEqual(result, r"C:\new\folder")

    def test_windows_path_in_replacement_case_insensitive(self):
        result, _ = lpb.apply_replacement("old", r"C:\new\folder", "OLD",
                                          regex=False, case=False)
        self.assertEqual(result, r"C:\new\folder")

    def test_newline_escape_not_expanded_case_sensitive(self):
        result, _ = lpb.apply_replacement("X", r"a\nb", "X",
                                          regex=False, case=True)
        self.assertEqual(result, r"a\nb")

    def test_newline_escape_not_expanded_case_insensitive(self):
        result, _ = lpb.apply_replacement("x", r"a\nb", "X",
                                          regex=False, case=False)
        self.assertEqual(result, r"a\nb")


# =============================================================================
# 4. rename_segment_walk — segment-by-segment rename algorithm
# =============================================================================

def _walk(mapping):
    """Run the walk with a call-recording rename_fn. Returns (calls, done)."""
    calls = []
    _, _, done = lpb.rename_segment_walk(mapping, lambda s, d: calls.append((s, d)))
    return calls, done


class TestRenameSegmentWalk(unittest.TestCase):

    # --- basic cases ---

    def test_single_leaf_rename(self):
        calls, _ = _walk([(r"A\B\C", r"A\B\CC")])
        self.assertEqual(calls, [(r"A\B\C", r"A\B\CC")])

    def test_middle_segment_rename(self):
        calls, _ = _walk([(r"A\B\C", r"A\BB\C")])
        self.assertEqual(calls, [(r"A\B", r"A\BB")])

    def test_two_segments_changed(self):
        calls, _ = _walk([(r"A\B\C\D", r"A\B\CC\DD")])
        self.assertEqual(calls, [
            (r"A\B\C",    r"A\B\CC"),
            (r"A\B\CC\D", r"A\B\CC\DD"),
        ])

    def test_identical_orig_new_fires_no_rename(self):
        calls, _ = _walk([(r"A\B\C", r"A\B\C")])
        self.assertEqual(calls, [])

    # --- depth mismatch is silently skipped ---

    def test_depth_change_skipped(self):
        calls, _ = _walk([(r"A\B\C", r"A\B\C\D")])
        self.assertEqual(calls, [])

    def test_depth_change_counted_as_fail(self):
        _, fail, _ = lpb.rename_segment_walk(
            [(r"A\B\C", r"A\B\C\D")], lambda s, d: None)
        self.assertEqual(fail, 1)

    # --- deduplication: shared parent renamed exactly once ---

    def test_shared_parent_renamed_once(self):
        mapping = [
            (r"A\B\C\file1.txt", r"A\B\CC\file1.txt"),
            (r"A\B\C\file2.txt", r"A\B\CC\file2.txt"),
        ]
        calls, _ = _walk(mapping)
        parent_renames = [(s, d) for s, d in calls if s == r"A\B\C"]
        self.assertEqual(len(parent_renames), 1,
                         "parent rename must fire exactly once for all siblings")

    def test_siblings_need_no_extra_rename_after_parent(self):
        mapping = [
            (r"A\B\C\X", r"A\B\CC\X"),
            (r"A\B\C\Y", r"A\B\CC\Y"),
        ]
        calls, done = _walk(mapping)
        self.assertEqual(len(calls), 1)
        self.assertEqual(done[r"A\B\C"], r"A\B\CC")

    # --- cross-row reconciliation ---

    def test_reconciliation_routes_through_renamed_parent(self):
        # Row A renames E -> EE; row B was built with the old E name.
        # The walk must route the F rename through the reconciled EE path.
        mapping = [
            (r"A\B\C\D\E",     r"A\B\C\D\EE"),
            (r"A\B\C\D\E\F\G", r"A\B\C\D\E\FF\G"),
        ]
        calls, _ = _walk(mapping)
        f_renames = [(s, d) for s, d in calls
                     if "F" in s.split(os.sep)[-1] or "F" in d.split(os.sep)[-1]]
        self.assertTrue(f_renames, "expected at least one rename involving F")
        for src, _ in f_renames:
            self.assertIn("EE", src,
                          f"F rename must go through reconciled EE path, got {src!r}")

    def test_reconciliation_does_not_double_rename_shared_segment(self):
        mapping = [
            (r"A\B\C\D\E",     r"A\B\C\D\EE"),
            (r"A\B\C\D\E\F\G", r"A\B\C\D\E\FF\G"),
        ]
        calls, _ = _walk(mapping)
        e_renames = [c for c in calls if c[0].endswith(r"\E")]
        self.assertEqual(len(e_renames), 1,
                         "E -> EE rename should fire only once even with two rows")

    # --- ordering: parents before children ---

    def test_parents_renamed_before_their_children(self):
        mapping = [
            (r"A\B\C\D\leaf", r"A\B\CC\D\leaf"),   # depth 5, listed first
            (r"A\B\C",        r"A\B\CC"),            # depth 3
        ]
        calls, _ = _walk(mapping)
        srcs = [s for s, _ in calls]
        self.assertIn(r"A\B\C", srcs)
        c_idx = srcs.index(r"A\B\C")
        for i, (s, _) in enumerate(calls):
            if "CC" in s:
                self.assertGreater(i, c_idx,
                                   f"path through CC at index {i} must come after parent at {c_idx}")

    def test_mapping_sorted_by_depth_before_processing(self):
        mapping = [
            (r"A\B\C\D\E", r"A\B\C\D\EE"),
            (r"A\B",       r"A\BB"),
            (r"A\B\C",     r"A\BB\C"),
        ]
        calls, _ = _walk(mapping)
        ab_idx = next(i for i, (s, _) in enumerate(calls) if s == r"A\B")
        for i, (s, _) in enumerate(calls):
            if "BB" in s:
                self.assertGreater(i, ab_idx)

    # --- ok/fail counts ---

    def test_successful_renames_counted(self):
        ok, fail, _ = lpb.rename_segment_walk(
            [(r"A\B\C", r"A\B\CC"), (r"A\B\D", r"A\B\DD")],
            lambda s, d: None)
        self.assertEqual(ok, 2)
        self.assertEqual(fail, 0)

    def test_failing_rename_counted_and_row_aborted(self):
        def bad_rename(s, d):
            raise OSError("disk full")

        ok, fail, _ = lpb.rename_segment_walk(
            [(r"A\B\C", r"A\B\CC")], bad_rename)
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
    sys.exit()
