# Tests

Two groups, deliberately kept apart:

- **`tests/client/`** -- plain async tests (`pytest` + `pytest-asyncio` +
  `respx`) for `custom_components/westerville_utilities/client/`, the
  portal-scraping/parsing layer. No Home Assistant dependency. Runs
  anywhere, including native Windows:

  ```
  uv sync
  uv run pytest tests/client/ -v
  ```

- **`tests/ha/`** -- tests that need a real `HomeAssistant` core object
  (`pytest-homeassistant-custom-component`'s `hass` fixture): coordinator,
  config flow, sensor entities. **Linux only** -- see below.

  ```
  uv sync --group ha-test
  uv run pytest tests/ha/ -v
  ```

## Why `tests/ha/` doesn't run on Windows

`pytest-homeassistant-custom-component` is deliberately its own dependency
group (`ha-test`), not part of the default `dev` group, and the two test
groups are never run in the same `pytest` invocation. This isn't just
organization -- it's load-bearing:

1. Home Assistant core (`homeassistant/runner.py`) imports `fcntl` and
   `resource` at module level, both POSIX-only. Nothing in this test suite
   actually needs them (they back a file lock and a file-descriptor-limit
   call that only matter for the *real* daemon entrypoint, which the
   `hass` fixture never goes through) -- but the bare `import` still fails
   without the real module on Windows.
2. Worse: `pytest-homeassistant-custom-component`'s plugin auto-registers
   itself with pytest the moment it's *installed* (via a `pytest11`
   setuptools entry point), regardless of which tests you actually select
   or any `-p no:...` flag -- so its module-level `fcntl`/`resource`
   imports run for *every* pytest invocation in a venv that has it
   installed, not just tests that use `hass`.
3. Once loaded, it also forces Home Assistant's own asyncio event loop
   policy globally, which on Windows collides with the test harness's
   network-safety guard (`pytest-socket`): Windows' default event loop
   needs a real `socket.socketpair()` internally just to start up, and the
   guard can't tell that apart from a test actually hitting the network.

(1) and (2) are why `ha-test` is a separate, not-installed-by-default
group at all -- so a plain `uv sync` for `tests/client/` never pulls in a
package whose mere presence poisons the whole venv. (3) is a deeper
Windows/asyncio interaction inside Home Assistant's own runner that isn't
practically patchable from here; this matches Home Assistant's own
contributor guidance to use WSL2/a devcontainer/Linux CI for running its
test suite, not native Windows.

If you do have a Linux environment (WSL2, Docker, CI), `tests/ha/` just
works there with no workarounds -- see `.github/workflows/test.yml` for
the environment CI uses. `tests/_windows_shims/` has small `fcntl.py` /
`resource.py` stubs that get you *past* problem (1) if you want to
experiment with `tests/ha/` locally on Windows anyway (`PYTHONPATH=tests/_windows_shims`)
-- but they don't solve (3), so a real `hass`-fixture test will still hit
the event loop wall.
