# Literate AI 0.8.1 (historical release marker)

Literate AI 0.8.1 was released on 2026-08-31 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `203f3641a0f92950e45c0a6c7cdb1a31fb432018`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.8.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.8.1 - 2026-09-01

- **Windows support restored (0.8.0 known issue fixed).** The 0.8.0 "manifest
  lock failed" failure on Windows was not the lock: `_replace` called
  `os.fsync` on a directory handle, which Windows rejects (`FlushFileBuffers`
  is invalid on a directory), and it surfaced through the lock's error handler.
  Guard the directory fsync to POSIX; converge the manifest lock acquire on the
  proven cache-lock primitive (write the placeholder byte before locking, no
  fsync/`O_CLOEXEC` before `LockFile`); and retry the Windows delete-pending
  `EACCES` on concurrent lock-file open. `litai init` and repository updates now
  work on Windows (full Windows CI green).
- Fix `litai project validate` DOC-IDENTITY advisory paths to use forward
  slashes on Windows.
- Make the release wheel-asset upload idempotent (`gh release upload --clobber`)
  so a retried or resumed publish does not report a false
  `release.wheel_upload_failed` after the wheel is already attached.
