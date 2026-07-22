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
     - Owner: `MartinGallagher-code`  ·  Repository: `martin_commander`
     - Workflow name: `publish.yml`  ·  Environment: `pypi`
     The included GitHub Actions workflow then publishes automatically when
     you push a version tag — nothing else to configure.
   - **API token:** create a token on PyPI (*Account settings → API tokens*)
     and either export it for twine (`TWINE_USERNAME=__token__`,
     `TWINE_PASSWORD=pypi-...`) or store it as a GitHub Actions secret.

## Releasing a new version

1. Bump the version in **both** places (keep them identical):
   - `pyproject.toml` → `[project] version`
   - `meridian_commander/__init__.py` → `__version__`
2. Run the checks locally:

   ```bash
   pip install -e ".[dev]"
   pytest
   python -m build            # builds sdist + wheel into dist/
   twine check dist/*
   ```

3. Commit, tag and push:

   ```bash
   git tag v1.0.0
   git push origin main v1.0.0
   ```

   With trusted publishing configured, the `publish.yml` workflow runs the
   test suite, builds, and uploads to PyPI on the tag push.

## Publishing manually instead

```bash
python -m build
twine upload dist/*                          # real PyPI
# or rehearse first:
twine upload --repository testpypi dist/*
pip install -i https://test.pypi.org/simple/ meridian-commander
```

## Notes

- The core has **no runtime dependencies**; `paramiko` is an optional extra
  (`pip install "meridian-commander[ssh]"`) needed for SFTP/SSH panes, the
  remote in-pane terminal and the SSH plug-ins.
- The app needs the `curses` module, so wheels are pure-Python but the tool
  targets POSIX systems (Linux, macOS). On Windows it runs under WSL.
- `scripts/merge.sh` / `scripts/split.sh` are repo utilities and ship in the
  sdist only, not in the wheel.
