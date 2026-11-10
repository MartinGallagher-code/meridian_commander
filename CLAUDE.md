# Notes for Claude

Meridian Commander is a two-pane terminal file manager in pure Python,
published on PyPI as `meridian-commander`; the repository and the package are
`meridian_commander`. `README.md` is the front page, `docs/` is the manual
(Sphinx, on Read the Docs), and `CONTRIBUTING.md` says what a change has to
meet: read it before changing anything.

## Every task is a ticket

Everything done here goes through a ticket in `MartinGallagher-code/shared_tools`,
which keeps every project's tickets as files and moves each one Backlog → In Progress
→ Review → Done (Adversarial Test only when the owner asks). Before any change:

1. Attach `MartinGallagher-code/shared_tools` with `add_repo` and push access, and
   clone it. If a clone is already here, pull it instead
   (`git -C <shared_tools clone> pull --ff-only`): a stale clone has a stale
   `CLAUDE.md` and a stale `ticket.py`.
2. Read the "Every task is a ticket" section of its `CLAUDE.md`. It sets out what each
   column requires, and none of it is optional.
3. Open the ticket with the shared_tools clone's script, not a path in this
   repository:

   ~~~bash
   <shared_tools clone>/scripts/ticket.py new meridian_commander "Imperative title" --request "the request, in the user's own words"
   ~~~

**Every change moves the version**, however small: a pull request into `main` raises
it in every place it is written and opens `CHANGELOG.md` with that version's own
dated section, never an "Unreleased" heading left for later. CI fails a pull request
whose version is not greater than its base branch's. This file's own section on the
version says where it is written.

The board: https://claude.ai/artifact/HrB3RpVpqGGQS7UZgZsMJm

## Checks

The four in `CONTRIBUTING.md`, the version check, and the manual's build. CI
(`.github/workflows/publish.yml`) runs all of them on every pull request:

```bash
pip install -e ".[dev]" -r docs/requirements.txt
pytest
coverage run -m pytest && coverage report    # fail_under = 100 (Linux, 3.10+)
ruff check .
mypy meridian_commander
bash tests/check_version.sh origin/main      # the version moved past main's
python -m sphinx -b html -W docs docs/_build/html
```

## Every change moves the version

The owner's rule (shared_tools BN-002): **whenever this project changes, its version
changes with it**, however small the change: code, tests, docs or CI. A pull request
into `main` carries its own bump and its own `CHANGELOG.md` section, never an
"Unreleased" heading left for later.

- Patch for fixes, docs, tests and CI; minor for anything new a user can use; major
  only when something that worked stops working the way it did.
- In the same pull request: `pyproject.toml` (`version`) and
  `meridian_commander/__init__.py` (`__version__`), and a `## [X.Y.Z] — YYYY-MM-DD`
  section at the top of `CHANGELOG.md`, with its link at the foot of the file.
  `tests/check_version.sh` fails if any of them disagree.
- The **version** job in CI fails a pull request whose version is not greater than its
  base branch's, and a push to `main` whose version is not greater than what `main` had
  before it. If `main` moves while a pull request is open, rebase and bump again.
- Publishing is a separate step, and a person's call: a `v*` tag, a manual dispatch,
  or a merge with `[release]` in its commit message runs `publish.yml`'s publish job
  (`PUBLISHING.md`). A number that is never published is fine; a change with no
  number is not.

## Bundling this repo

Use the canonical scripts in `MartinGallagher-code/shared_tools` and read its
`CLAUDE.md` first. `scripts/handover.sh` reads `repos/meridian_commander.conf`,
which drops `docs/` and sends 20 files under `tests/` as base64: an upload
scanner refused them in the clear (PEM private-key headers used as fixtures,
and ssh tests shaped like a dropper). Export with `git archive`, never a copy
of a working tree: `.git/` never goes into a bundle.
