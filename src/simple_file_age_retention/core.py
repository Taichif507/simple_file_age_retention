"""Core implementation of file-age-based retention.

Design decisions:
- ``mtime`` is the age signal. It is portable, cheap, and matches what users
  expect when they think "how old is this file?". atime is unreliable (many
  systems mount with noatime) and ctime changes on metadata edits, which would
  surprise users who only renamed a file.
- Age is compared as ``now - mtime >= threshold_seconds``. A file whose mtime
  equals ``now`` has age 0 and is never deleted for any positive threshold.
- The directory itself is never deleted; only its direct file entries.
  Recursing into subdirectories is intentionally out of scope — one job,
  one clear rule.
- Symlinks are never followed and never deleted. Removing a symlink can break
  unrelated systems, and following one can delete a file outside the target
  directory. We skip them explicitly so the behaviour is predictable.
- A ``clock`` callable is injected so tests are deterministic. Production code
  passes ``time.time``; tests pass a fake.
- Deletion failures are collected per-file rather than aborting the run, so a
  single locked file does not prevent cleanup of the rest.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List


@dataclass(frozen=True)
class RetentionReport:
    """Per-file outcome of a retention scan.

    ``age_seconds`` is ``None`` for symlinks and entries that could not be
    stat'd, because in those cases there is no meaningful mtime to report.
    """

    path: str
    age_seconds: float
    would_delete: bool
    deleted: bool
    error: str


@dataclass(frozen=True)
class RetentionResult:
    """Aggregate outcome of a retention run."""

    deleted: List[str] = field(default_factory=list)
    retained: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[RetentionReport] = field(default_factory=list)


class RetentionScanner:
    """Scan a directory and delete files older than a threshold.

    Parameters
    ----------
    directory:
        Directory whose direct file entries are considered. The directory
        itself is never deleted.
    threshold_seconds:
        Files whose age (``now - mtime``) is greater than or equal to this
        value are eligible for deletion. Must be non-negative.
    dry_run:
        When ``True``, report what would be deleted without deleting it.
    clock:
        Callable returning the current time as epoch seconds. Injected so
        tests can be deterministic. Defaults to :func:`time.time`.
    """

    def __init__(
        self,
        directory: str,
        threshold_seconds: float,
        *,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if threshold_seconds < 0:
            raise ValueError("threshold_seconds must be non-negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._directory = directory
        self._threshold_seconds = threshold_seconds
        self._dry_run = dry_run
        self._clock = clock

    @property
    def directory(self) -> str:
        return self._directory

    @property
    def threshold_seconds(self) -> float:
        return self._threshold_seconds

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def run(self) -> RetentionResult:
        """Run the retention pass and return a :class:`RetentionResult`."""
        result = RetentionResult()
        now = self._clock()

        try:
            entries = os.listdir(self._directory)
        except OSError as exc:
            # Surface the directory-level failure on every report so callers
            # can see why nothing was processed.
            result.errors.append(
                RetentionReport(
                    path=self._directory,
                    age_seconds=0.0,
                    would_delete=False,
                    deleted=False,
                    error=f"cannot list directory: {exc}",
                )
            )
            return result

        for name in entries:
            full = os.path.join(self._directory, name)

            # Symlinks: never follow, never delete. See module docstring.
            if os.path.islink(full):
                result.skipped.append(full)
                continue

            try:
                st = os.stat(full)
            except OSError:
                # File vanished between listdir and stat, or is unreadable.
                # Report and move on; one bad entry must not stop the run.
                result.errors.append(
                    RetentionReport(
                        path=full,
                        age_seconds=0.0,
                        would_delete=False,
                        deleted=False,
                        error="cannot stat file",
                    )
                )
                continue

            # Only plain files are candidates. Directories and special files
            # are left untouched — deletion of a subdirectory tree is a
            # separate, riskier operation outside this library's scope.
            if not os.path.isfile(full):
                result.skipped.append(full)
                continue

            age = now - st.st_mtime
            should_delete = age >= self._threshold_seconds

            if not should_delete:
                result.retained.append(full)
                continue

            if self._dry_run:
                result.deleted.append(full)
                continue

            try:
                os.remove(full)
                result.deleted.append(full)
            except OSError as exc:
                result.errors.append(
                    RetentionReport(
                        path=full,
                        age_seconds=age,
                        would_delete=True,
                        deleted=False,
                        error=str(exc),
                    )
                )

        return result
