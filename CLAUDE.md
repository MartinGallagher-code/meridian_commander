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

The board: https://claude.ai/artifact/HrB3RpVpqGGQS7UZgZsMJm

## Checks

The four in `CONTRIBUTING.md`, and the manual's build. CI
(`.github/workflows/publish.yml`) runs all of them on every pull request:

```bash
pip install -e ".[dev]" -r docs/requirements.txt
pytest
coverage run -m pytest && coverage report    # fail_under = 100 (Linux, 3.10+)
ruff check .
mypy meridian_commander
python -m sphinx -b html -W docs docs/_build/html
```

Every pull request adds an entry under `## [Unreleased]` in `CHANGELOG.md`
and does not bump the version: releases are cut separately (`PUBLISHING.md`).

## Bundling this repo

Use the canonical scripts in `MartinGallagher-code/shared_tools` and read its
`CLAUDE.md` first. `scripts/handover.sh` reads `repos/meridian_commander.conf`,
which drops `docs/` and sends 20 files under `tests/` as base64: an upload
scanner refused them in the clear (PEM private-key headers used as fixtures,
and ssh tests shaped like a dropper). Export with `git archive`, never a copy
of a working tree: `.git/` never goes into a bundle.
