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

1. Bump the version in **both** places (keep them identical — the test suite
   checks that they are, and PyPI refuses a version it already has):
   - `pyproject.toml` → `[project] version`
   - `meridian_commander/__init__.py` → `__version__`
2. Move everything under `## [Unreleased]` in `CHANGELOG.md` into a new
   section for the version, dated today, and add the link at the foot of the
   file.
3. Run the checks locally:

   ```bash
   pip install -e ".[dev]"
   pytest
   coverage run -m pytest && coverage report
   ruff check .
   mypy meridian_commander
   python -m build            # builds sdist + wheel into dist/
   twine check dist/*
   ```

4. Commit, tag and push:

   ```bash
   git tag v1.0.0
   git push origin main v1.0.0
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
workflow asks PyPI first and stops with a clear message rather than failing
inside twine.

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
