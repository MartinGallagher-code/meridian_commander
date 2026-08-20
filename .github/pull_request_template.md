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

- [ ] Added an entry under `## [Unreleased]` in `CHANGELOG.md`
- [ ] Did not bump the version (releases are cut separately)

<!--
If the change touches drawing or key handling, say which terminal you tried it
in. If it touches a remote backend, say whether you tested against a real
server as well as the suite.
-->
