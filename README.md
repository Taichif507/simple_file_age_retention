# simple_file_age_retention

Deletes files in a directory older than a given age threshold, with optional dry-run and a structured report.

```python
from simple_file_age_retention import RetentionScanner

scanner = RetentionScanner("/var/log/app", threshold_seconds=86400, dry_run=True)
result = scanner.run()
print("would delete:", result.deleted)
print("keeping:", result.retained)
print("skipped:", result.skipped)
print("errors:", result.errors)
```

`RetentionScanner(directory, threshold_seconds, *, dry_run=False, clock=time.time)` scans a directory's direct entries, deletes plain files whose `now - mtime >= threshold_seconds`, and returns a `RetentionResult` with `deleted`, `retained`, `skipped`, and `errors` lists. `errors` holds `RetentionReport` instances (path, age_seconds, would_delete, deleted, error) for any file that could not be stat'd or removed.

## Why

Log and cache directories accumulate files. Cron jobs that delete "old" files usually shell out to `find`, which works until someone passes a wrong flag or a symlink sends it walking into the wrong tree. This library gives you one small, tested Python object with predictable rules and a report you can log.

## Decisions

- **`mtime` is the age signal.** `atime` is unreliable (many mounts use `noatime`), and `ctime` changes on metadata edits, which would surprise users who only renamed a file.
- **The threshold is inclusive.** A file exactly `threshold_seconds` old is deleted. A file one second younger is kept.
- **Symlinks are never followed and never deleted.** Following a symlink can delete a file outside the target directory; deleting one can break an unrelated system. They appear in `skipped`.
- **Only direct file entries are considered.** Subdirectories are skipped, not recursed into. Recursive deletion is a separate, riskier operation.
- **A `clock` callable is injected.** Default is `time.time`. Pass your own in tests so they never depend on wall-clock time.

## Edge you will hit

If a file cannot be removed (permissions, locks), it is recorded in `result.errors` and the run continues. The file is **not** added to `deleted`. Check `errors` if you need to know cleanup was incomplete.
