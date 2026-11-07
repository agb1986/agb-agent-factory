# Changelog fragments

Each pull request that changes something a user notices adds one file here instead of editing `CHANGELOG.md`, so two pull requests never touch the same line of the changelog. At release time `python scripts/collect_changelog.py` moves every fragment under `## [Unreleased]` and deletes it; the steps are in [docs/releasing.md](../docs/releasing.md).

Name the file `<id>.<section>.md`: `id` is the number of the issue the change closes, or a short slug when there is none, and `section` is one of `added`, `changed`, `deprecated`, `removed`, `fixed` or `security`. One pull request may add several fragments. The content is the bullet line, or lines, exactly as they go into the changelog: each entry starts with `- `, is written for a user rather than as a commit title, and ends with the issue number in parentheses, for example:

```
- `deps.graph` and `deps.impact`, read-only tools that return a repository's import graph (#88).
```

Check what a release would gain with `python scripts/collect_changelog.py --dry-run`.
