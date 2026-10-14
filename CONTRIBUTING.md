# Contributing

Start with [README.md](README.md) and reproduce the issue using the documented entry point.

1. Open a focused issue with the version/commit, environment, expected behavior and actual result. Use synthetic or redacted data.
2. Keep each pull request focused. Explain user-visible behavior, affected entry points, and verification results.
3. Separate documentation/appearance changes from business logic, configuration contracts, retries, permissions, and external side effects.
4. Preserve existing work and third-party attribution. Do not add credentials, account data, local paths, caches, build output, or large runtime captures.
5. Run the checks relevant to the changed files. State unavailable environments and untested integration paths explicitly.

Submitting code does not authorize packaging, deploying, publishing releases, or operating external accounts. These actions require their own project approval.

## Review checklist

- Instructions and relative links match the committed tree.
- Examples work from a fresh checkout and identify required external inputs.
- Tests cover meaningful behavior, and reported results match the current commit.
- No unfinished feature is described as complete.
- Existing license notices remain intact; do not assume a license for files without one.
