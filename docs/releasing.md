# Releasing

A release is a tagged commit on `main` with a matching `CHANGELOG.md` section, a GitHub release, and a rebuilt Docker image. The changelog follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the version follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every change goes under `## [Unreleased]` in the pull request that makes it, so a release only moves text.

The version is written once, in `pyproject.toml`. `factory --version` and `docker run --rm agb-agent-factory --version` read it from the installed package metadata, so no other file carries a version literal.

## What qualifies as a release

- **Patch** (`0.1.1`): a fix that changes no behaviour a caller depends on, a prompt or eval change that leaves the agents' output contract alone, or a documentation or packaging fix.
- **Minor** (`0.2.0`): a new agent, a new pipeline, a new CLI verb, or a new option that existing callers can ignore.
- **Major** (`1.0.0`): a change that breaks a CLI verb, an exit code, the trace store or the output contract of a pipeline.

Agent manifests carry their own semver, separate from the package version. An agent's `name@^MAJOR.MINOR` range and its bump rules are in [CONVENTIONS.md](CONVENTIONS.md#agent-address). A package release does not bump an agent version, and an agent version bump does not force a package release.

The first tag, `v0.1.0`, is cut only when every item in [HANDOVER-v2.md](HANDOVER-v2.md) section 6 is met.

## Steps

Run each step from the repository root on a clean checkout of `main`. The version used below is `X.Y.Z`, the date is `YYYY-MM-DD`, and `PREV` is the previous tag, or `v0.0.0` before the first one.

1. Confirm `main` is green and the tree is clean:

   ```sh
   git switch main && git pull --ff-only
   gh run list --branch main --workflow ci --limit 1
   git status --short            # must print nothing
   ```

2. Bump `version` in `pyproject.toml` to `X.Y.Z`, then refresh the lock file:

   ```sh
   uv lock
   ```

3. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add an empty `## [Unreleased]` above it with no entries under it, and point the links at the bottom of the file. Replace the `[Unreleased]` link with the tag comparison and add a link for the new version:

   ```
   [Unreleased]: https://github.com/agb1986/agb-agent-factory/compare/vX.Y.Z...main
   [X.Y.Z]: https://github.com/agb1986/agb-agent-factory/compare/PREV...vX.Y.Z
   ```

   Remove the comment above the link block that says the link points at `commits/main`. For the first tag, `PREV` does not exist, so `[X.Y.Z]` links to `releases/tag/vX.Y.Z` instead.

4. Commit the bump:

   ```sh
   git add pyproject.toml uv.lock CHANGELOG.md
   git commit -m "chore(release): vX.Y.Z"
   ```

5. Tag the commit:

   ```sh
   git tag -a vX.Y.Z -m "vX.Y.Z"
   ```

6. Push the commit and the tag together:

   ```sh
   git push origin main --follow-tags
   ```

7. Extract the release section from the changelog and create the GitHub release from it:

   ```sh
   notes="$(mktemp)"
   awk '/^## \[X\.Y\.Z\]/{on=1; next} /^## \[/{on=0} on' CHANGELOG.md > "$notes"
   gh release create vX.Y.Z --title vX.Y.Z --notes-file "$notes"
   rm "$notes"
   ```

8. Rebuild the image and check its version, as in [deploy.md](deploy.md):

   ```sh
   docker build -t agb-agent-factory .
   docker run --rm agb-agent-factory --version     # factory X.Y.Z
   ```

9. Start the next cycle: the `## [Unreleased]` section stays in `CHANGELOG.md` for the next pull request to fill.

If a step fails after step 5, the tag exists locally and may exist on the remote. Do not move a tag that has been pushed. Delete it with `git tag -d vX.Y.Z` and `git push origin :refs/tags/vX.Y.Z` only if no release was created, then fix the cause and start again from step 2.
