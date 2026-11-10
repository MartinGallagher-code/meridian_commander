<!--
One change per pull request, please. A lint sweep and a new feature in the
same branch make both harder to review.
-->

## What this changes

<!-- What it does, and why the old behaviour was wrong or insufficient. -->

## Checks

<!-- All four are what CI runs. Ticking a box you did not run wastes a cycle. -->

- [ ] `pytest`
- [ ] `coverage run -m pytest && coverage report` — still 100%
- [ ] `ruff check .`
- [ ] `mypy meridian_commander`

## Notes for the reviewer

- [ ] Moved the version in `pyproject.toml` and `meridian_commander/__init__.py`,
      however small the change (patch for fixes, docs, tests and CI; minor for
      anything new)
- [ ] Opened `CHANGELOG.md` with that version's own `## [X.Y.Z] — YYYY-MM-DD`
      section, never an "Unreleased" one, and its link at the foot
- [ ] `bash tests/check_version.sh origin/main` passes

<!--
If the change touches drawing or key handling, say which terminal you tried it
in. If it touches a remote backend, say whether you tested against a real
server as well as the suite.
-->
