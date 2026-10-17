import os
import stat
import tempfile
import time
import unittest
from typing import List

from simple_file_age_retention import RetentionReport, RetentionResult, RetentionScanner


class _FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TempDir:
    """Context manager that creates files with fixed mtimes."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix="sfr_test_")
        self._cleanup: List[str] = [self.path]

    def _abs(self, name: str) -> str:
        return os.path.join(self.path, name)

    def make_file(self, name: str, mtime: float, content: str = "x") -> str:
        full = self._abs(name)
        with open(full, "w") as fh:
            fh.write(content)
        os.utime(full, (mtime, mtime))
        return full

    def make_symlink(self, name: str, target: str) -> str:
        full = self._abs(name)
        os.symlink(target, full)
        return full

    def make_subdir(self, name: str) -> str:
        full = self._abs(name)
        os.mkdir(full)
        self._cleanup.append(full)
        return full

    def exists(self, name: str) -> bool:
        return os.path.exists(self._abs(name))

    def cleanup(self) -> None:
        for p in reversed(self._cleanup):
            try:
                if os.path.islink(p):
                    os.unlink(p)
                elif os.path.isdir(p):
                    os.rmdir(p)
                else:
                    os.unlink(p)
            except OSError:
                pass


class TestRetentionScanner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _TempDir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- construction & validation -------------------------------------

    def test_negative_threshold_raises(self) -> None:
        with self.assertRaises(ValueError):
            RetentionScanner(self.tmp.path, -1)

    def test_non_callable_clock_raises(self) -> None:
        with self.assertRaises(TypeError):
            RetentionScanner(self.tmp.path, 60, clock=42)  # type: ignore[arg-type]

    def test_properties_reflect_arguments(self) -> None:
        clock = _FakeClock()
        s = RetentionScanner(self.tmp.path, 120, dry_run=True, clock=clock)
        self.assertEqual(s.directory, self.tmp.path)
        self.assertEqual(s.threshold_seconds, 120)
        self.assertTrue(s.dry_run)

    # --- happy path ----------------------------------------------------

    def test_deletes_old_file_keeps_new_file(self) -> None:
        clock = _FakeClock()
        # old: 100 seconds ago; new: 10 seconds ago
        old = self.tmp.make_file("old.txt", clock.now - 100)
        new = self.tmp.make_file("new.txt", clock.now - 10)

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.deleted, [old])
        self.assertEqual(result.retained, [new])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors, [])
        self.assertFalse(self.tmp.exists("old.txt"))
        self.assertTrue(self.tmp.exists("new.txt"))

    def test_dry_run_does_not_delete(self) -> None:
        clock = _FakeClock()
        old = self.tmp.make_file("old.txt", clock.now - 100)

        result = RetentionScanner(
            self.tmp.path, 60, dry_run=True, clock=clock
        ).run()

        self.assertEqual(result.deleted, [old])
        self.assertTrue(self.tmp.exists("old.txt"))

    # --- edge: boundary is inclusive -----------------------------------

    def test_file_exactly_at_threshold_is_deleted(self) -> None:
        clock = _FakeClock()
        edge = self.tmp.make_file("edge.txt", clock.now - 60)

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.deleted, [edge])
        self.assertFalse(self.tmp.exists("edge.txt"))

    def test_file_just_below_threshold_is_kept(self) -> None:
        clock = _FakeClock()
        young = self.tmp.make_file("young.txt", clock.now - 59)

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.retained, [young])
        self.assertTrue(self.tmp.exists("young.txt"))

    def test_zero_threshold_deletes_everything(self) -> None:
        clock = _FakeClock()
        a = self.tmp.make_file("a.txt", clock.now)
        b = self.tmp.make_file("b.txt", clock.now - 1000)

        result = RetentionScanner(self.tmp.path, 0, clock=clock).run()

        self.assertEqual(sorted(result.deleted), sorted([a, b]))
        self.assertFalse(self.tmp.exists("a.txt"))
        self.assertFalse(self.tmp.exists("b.txt"))

    # --- edge: non-file entries are skipped ----------------------------

    def test_symlinks_are_skipped_and_never_deleted(self) -> None:
        clock = _FakeClock()
        real = self.tmp.make_file("real.txt", clock.now - 100)
        link = self.tmp.make_symlink("link.txt", os.path.join(self.tmp.path, "real.txt"))

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.skipped, [link])
        self.assertEqual(result.deleted, [real])
        self.assertFalse(self.tmp.exists("real.txt"))
        self.assertTrue(os.path.islink(link))

    def test_subdirectories_are_skipped(self) -> None:
        clock = _FakeClock()
        sub = self.tmp.make_subdir("subdir")
        # The subdir is old, but must not be touched.
        os.utime(sub, (clock.now - 1000, clock.now - 1000))

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.skipped, [sub])
        self.assertTrue(os.path.isdir(sub))

    # --- edge: empty and missing directories ---------------------------

    def test_empty_directory_reports_nothing(self) -> None:
        clock = _FakeClock()
        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        self.assertEqual(result.deleted, [])
        self.assertEqual(result.retained, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors, [])

    def test_nonexistent_directory_records_error(self) -> None:
        clock = _FakeClock()
        missing = os.path.join(self.tmp.path, "does_not_exist")

        result = RetentionScanner(missing, 60, clock=clock).run()

        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertEqual(err.path, missing)
        self.assertIn("cannot list directory", err.error)

    # --- edge: deletion failure is reported, not fatal ------------------

    def test_deletion_failure_collected_not_raised(self) -> None:
        clock = _FakeClock()
        target = self.tmp.make_file("locked.txt", clock.now - 100)

        original_remove = os.remove
        call_count = {"n": 0}

        def fake_remove(path: str) -> None:
            call_count["n"] += 1
            if os.path.samefile(path, target):
                raise PermissionError("simulated lock")
            original_remove(path)

        try:
            os.remove = fake_remove  # type: ignore[assignment]
            result = RetentionScanner(self.tmp.path, 60, clock=clock).run()
        finally:
            os.remove = original_remove  # type: ignore[assignment]

        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].path, target)
        self.assertIn("simulated lock", result.errors[0].error)
        self.assertTrue(self.tmp.exists("locked.txt"))

    # --- edge: stat failure --------------------------------------------

    def test_unstatable_file_recorded_as_error(self) -> None:
        clock = _FakeClock()
        victim = self.tmp.make_file("victim.txt", clock.now - 100)
        victim_abs = os.path.abspath(victim)
        original_stat = os.stat

        def fake_stat(path):
            if os.path.abspath(path) == victim_abs:
                raise OSError("nope")
            return original_stat(path)

        try:
            os.stat = fake_stat  # type: ignore[assignment]
            result = RetentionScanner(self.tmp.path, 60, clock=clock).run()
        finally:
            os.stat = original_stat  # type: ignore[assignment]

        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].path, victim)
        self.assertEqual(result.errors[0].error, "cannot stat file")

    # --- multiple files: ordering follows listdir -----------------------

    def test_multiple_files_processed_in_listdir_order(self) -> None:
        clock = _FakeClock()
        names = ["c.txt", "a.txt", "b.txt"]
        for n in names:
            self.tmp.make_file(n, clock.now - 100)

        result = RetentionScanner(self.tmp.path, 60, clock=clock).run()

        # listdir order is filesystem-dependent, so we only assert that
        # exactly those three were deleted and none were retained.
        expected = sorted(os.path.join(self.tmp.path, n) for n in names)
        self.assertEqual(sorted(result.deleted), expected)
        self.assertEqual(result.retained, [])


if __name__ == "__main__":
    unittest.main()
