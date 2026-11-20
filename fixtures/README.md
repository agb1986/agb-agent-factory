# Fixtures

Small git repositories with planted defects. Agents and eval cases review them, and the
`defects.yaml` beside each one says what a correct review finds. The rules are in
[docs/CONVENTIONS.md](../docs/CONVENTIONS.md#fixtures).

## Build

```bash
uv run python -m fixtures.build                  # every fixture, about a second
uv run python -m fixtures.build mixed sql-injection
uv run python -m fixtures.build --list           # names only
uv run python -m fixtures.build --json           # {"fixtures": [{name, path, base, head}]}
```

Repositories land in `fixtures/repos/<name>/` (gitignored). Each has two commits, on the branches
`base` and `head`; `head` is checked out. A review diff is `git diff base head`. Author, committer
and dates are fixed, no hooks run and no git config is read, so rebuilding gives the same SHAs. A
rebuild deletes the old directory. Exit codes follow the usual convention: 3 for an unknown name.

## Layout

```text
fixtures/src/<name>/base/        tree of the first commit
fixtures/src/<name>/head/        tree of the second commit
fixtures/src/<name>/defects.yaml planted defects, with paths and lines in the head tree
fixtures/handmade/<name>/        same shape, for hand-built cases
fixtures/schema.py               pydantic model of defects.yaml and the checker
fixtures/build.py                the build script
```

The trees are ordinary Python projects whose own tests pass on both refs (a test that fails on
`head` would hand the reviewer the answer). The files are linted and type-checked with the rest of
the repository, so a planted defect must be one ruff and ty accept. The exceptions are
`off-by-one-red`, `mixed-red` and `sql-injection-red`, fixtures for the fixer agent: their `head` has
a test that fails on purpose, and `tests/fixtures` expects exactly that.

## Fixture names

`evals/cases.yaml` files in agents refer to a fixture by its directory name. **Never rename a
fixture**; add a new one instead.

| Name | What `head` changes | Planted defects |
| --- | --- | --- |
| `off-by-one` | Rewrites `rolling_sums` as a running sum with a loop bound one too small | 1 (major, logic) |
| `off-by-one-red` | `off-by-one`, plus `test_last_window_is_included`, which fails on `head` (a fixer eval: make it pass) | 1 (major, logic) |
| `clean-refactor` | Renames `calc` to `invoice_total`, extracts two helpers, same behaviour | none |
| `untested-change` | Adds the public `apply_discount` with no test | 1 (minor, testing) |
| `untested-change-covered` | `untested-change`, plus a passing test file for `apply_discount` (a fixer eval: nothing to fix) | none |
| `partly-covered` | Adds the public `restock` with no test, next to `reserve`, whose error branches are untested too (a test-writer cover-mode eval) | 1 (minor, testing) |
| `mixed-red` | `mixed`, plus `test_order_at_the_threshold_ships_free`, which fails on `head` (a fixer eval: fix `shipping.py` only) | 2 (major logic, info style) |
| `sql-injection-red` | `sql-injection`, plus a test that passes a hostile name to `find_by_name` and fails on `head` (a fixer eval) | 1 (blocker, security) |
| `mixed` | Three files: a `>=` flipped to `>` (bug), a camelCase local (nit), comment-only edits (harmless) | 2 (major logic, info style) |
| `docs-only` | Edits `README.md` and `docs/usage.md` | none |
| `test-only` | Adds two test cases | none |
| `sql-injection` | Adds `find_by_name`, which formats the name into the SQL text | 1 (blocker, security) |
| `n-plus-one` | Adds a function that runs one customer query per order | 1 (major, performance) |
| `off-by-one-single-file` | Hand-built (`fixtures/handmade/`): `last_n` as a loop that starts one item late, one file, no tests | 1 (major, logic) |
| `secret-in-config` | Adds a payments API token to the settings module and a charge helper that uses it | 1 (blocker, security) |
| `sast-false-positive` | Names the auth header and the token env var as constants and adds jittered retry waits; a static analyser flags all three, wrongly | none |
| `storefront` | Adds a line to the README on how to run the tests. A small shop package (money, catalog, cart, orders, API, CLI) whose modules import one another, for story-breakdown evals | none |
| `tracker` | Adds a line to the README on how to run the tests. A small issue tracker package (models, storage, search, report, CLI), for story-breakdown evals | none |

## `defects.yaml`

```yaml
fixture: sql-injection          # must equal the directory name
summary: One paragraph on what head changes.
defects:                        # [] for a fixture with nothing to find
  - id: name-interpolated-into-sql   # unique slug
    file: accounts/db.py             # relative to the head tree
    line_start: 30                   # 1-based, inclusive
    line_end: 31
    severity: blocker                # info | minor | major | blocker
    category: security               # slug: logic, style, security, performance, testing
    title: SQL injection through the name argument
    description: Why it is a defect and how to fix it.
    snippet: "WHERE name = '{name}'" # text that must sit inside the window
```

The build and the tests check that each file exists in the head tree, that the window ends inside
it and that the window contains the snippet. Edit the window whenever you move the code.

## Add a fixture

1. Create `fixtures/src/<name>/base/`, `head/` and `defects.yaml`. The name is lowercase words joined
   by hyphens.
2. Make sure the tests inside both trees pass, and that `ruff` and `ty` accept the code.
3. Run `uv run python -m fixtures.build <name>` and read `git -C fixtures/repos/<name> diff base head`.
4. Add the name and a one-line description to the table above.

No registration is needed: `discover` in `fixtures/build.py` finds every directory with a `base/`
and a `head/` under the source roots in `ROOTS`. `uv run pytest tests/fixtures` covers all of them.
