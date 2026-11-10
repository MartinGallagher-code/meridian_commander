# Publishing Meridian Commander to PyPI

The project is packaged with `pyproject.toml` (setuptools backend). The
distribution name is **`meridian-commander`**; the import package is
`meridian_commander`.

## One-time setup

1. Create an account on <https://pypi.org> (and <https://test.pypi.org> for
   rehearsals).
2. Pick **one** authentication route:
   - **Trusted publishing (recommended, no tokens):** on PyPI go to
     *Your projects → Publishing → Add a new pending publisher* and register:
     - PyPI project name: `meridian-commander`
     - Owner: `MartinGallagher-code`  ·  Repository: `meridian_commander`
     - Workflow name: `publish.yml`  ·  Environment: `pypi`
     The included GitHub Actions workflow then publishes automatically when
     you push a version tag — nothing else to configure.
   - **API token:** create a token on PyPI (*Account settings → API tokens*)
     and either export it for twine (`TWINE_USERNAME=__token__`,
     `TWINE_PASSWORD=pypi-...`) or store it as a GitHub Actions secret.

## Releasing a new version

There is nothing to bump at release time. Every pull request already moves
the version and opens `CHANGELOG.md` with that version's own dated section
(see "Pull requests" in [CONTRIBUTING.md](CONTRIBUTING.md)), and CI fails one
that does not. A release publishes the version `main` already carries.
Whether and when to publish is a maintainer's call, and a version that is
never published is fine.

1. Check the version on `main` is one PyPI does not have yet — it refuses a
   version it already has. It is written in **both** places, and the test
   suite and `tests/check_version.sh` check that they agree and that
   `CHANGELOG.md` opens with it:
   - `pyproject.toml` → `[project] version`
   - `meridian_commander/__init__.py` → `__version__`
2. Run the checks locally:

   ```bash
   pip install -e ".[dev]"
   pytest
   coverage run -m pytest && coverage report
   ruff check .
   mypy meridian_commander
   bash tests/check_version.sh
   python -m build            # builds sdist + wheel into dist/
   twine check dist/*
   ```

3. Tag that commit on `main` with its version, and push the tag:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

   With trusted publishing configured, the `publish.yml` workflow runs the
   test suite, builds, and uploads to PyPI on the tag push.

### Tag the release even when you cut it the other way

The workflow also publishes when a push to `main` carries `[release]` in its
commit message, which lets a release be cut by merging a pull request with
that marker in the squash title. That route is convenient and it skips the
tag, which is why releases 1.0.0 through 1.3.0 exist on PyPI but nowhere in
this repository's history — there is no `v1.2.0` to check out, diff against,
or link a changelog entry to.

If you release that way, tag the merge commit afterwards:

```bash
git tag v1.3.0 <the merge commit>
git push origin v1.3.0
```

Pushing a tag that names an already-published version is harmless: the
workflow asks PyPI first, and for a tag it logs a notice and skips the upload
rather than failing. (A manual dispatch or a `[release]` merge that cannot
upload still stops loudly — there, not being able to publish means someone
forgot to bump the version.)

### The four releases that were never tagged

1.0.0 through 1.3.0 are on PyPI and absent from this history. Each release
commit carries its own version in both `pyproject.toml` and `__init__.py`, so
the mapping is unambiguous:

```bash
git tag -a v1.0.0 be40a0d -m "meridian-commander 1.0.0"   # 2026-07-22
git tag -a v1.1.0 b49bd6f -m "meridian-commander 1.1.0"   # 2026-07-23
git tag -a v1.2.0 7c6792d -m "meridian-commander 1.2.0"   # 2026-08-10
git tag -a v1.3.0 552dc66 -m "meridian-commander 1.3.0"   # 2026-08-13
git push origin v1.0.0 v1.1.0 v1.2.0 v1.3.0
```

Expect each of those four pushes to run the workflow **as it stood at that
commit**, not as it stands now — so the skip-the-upload behaviour above does
not apply to them, and all four runs will go red. Nothing can actually be
republished (PyPI never allows a filename to be reused), but the 1.2.0 and
1.3.0 commits will stop at their own pre-check while 1.0.0 and 1.1.0, which
predate it, will attempt an upload and be refused by PyPI. That is a one-off
cost of backfilling; tags cut from here on behave properly.

Once they exist, the links at the foot of `CHANGELOG.md` can become ordinary
`compare/v1.2.0...v1.3.0` links instead of pointing at commit SHAs.

## Publishing manually instead

```bash
python -m build
twine upload dist/*                          # real PyPI
# or rehearse first:
twine upload --repository testpypi dist/*
pip install -i https://test.pypi.org/simple/ meridian-commander
```

## When the upload fails with "400 Bad Request"

twine reports the failure as a bare

```
ERROR    HTTPError: 400 Bad Request from https://upload.pypi.org/legacy/
         Bad Request
```

and prints the actual reason a few lines *above* that, inside the HTML page
PyPI returned. Scroll up in the job log. By far the most common reason is

```
400 File already exists ('meridian_commander-1.1.0-py3-none-any.whl', ...)
```

which means that version is already released. PyPI never allows a file name to
be reused — not even after the file is deleted — so the upload cannot be made
to succeed: bump the version and release again. The workflow now checks PyPI
before uploading and fails with that advice instead, so this should only be
reachable by uploading by hand.

## Notes

- The core has **no runtime dependencies**; `paramiko` is an optional extra
  (`pip install "meridian-commander[ssh]"`) needed for SFTP/SSH panes, the
  remote in-pane terminal and the SSH plug-ins.
- The app needs the `curses` module, so wheels are pure-Python but the tool
  targets POSIX systems (Linux, macOS). On Windows it runs under WSL.
- The bundle utilities (`merge.sh` / `split.sh`) live in the **shared_tools**
  repository and are not part of this package at all — neither the sdist nor
  the wheel ships them.
