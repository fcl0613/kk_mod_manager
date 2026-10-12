# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

KK Mod Manager is a PyQt6 desktop GUI for managing Sideloader `.zipmod` files for Illusion games (Koikatsu / Koikatsu Sunshine). It maintains a local mod repository, installs/removes mods to/from the game directory, and parses character/clothing card PNGs to identify missing mod dependencies.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Install dev/test dependencies (includes pytest)
pip install -r requirements-dev.txt

# Run the app (development)
python main.py

# Run the test suite
.venv/Scripts/python.exe -m pytest

# Package to .exe
pyinstaller KKModManager.spec
```

### Testing

pytest is configured in `pyproject.toml` (`[tool.pytest.ini_options]`). `python_files = ["test_*.py"]` is load-bearing: the default pattern also matches the three legacy manual scripts (`tests/*_test.py`), which must stay out of pytest collection.

- `tests/test_*.py` + `tests/conftest.py` — the real suite (services, utils, models, card parsing). Fixtures provide in-memory SQLite, tmp game/repo dirs, and an **autouse swap of `app.config._config_instance`** so no test ever touches real `data/` paths; zipmod/card fixtures are generated at runtime (`.gitignore` excludes `*.zipmod` and `data/`).
- `tests/sync_service_test.py` — manual script covering `sync_from_disk` (14 scenarios, run directly with `python tests/sync_service_test.py`); intentionally not migrated to pytest. `sync_from_disk`/`refresh_from_disk` are covered there, not in the pytest suite.
- `tests/card_service_test.py`, `tests/scene_service_test.py` — exploratory scratch scripts with hardcoded local paths and no assertions; they exercise no project code (there is no SceneService).

## Architecture

**Layered design**: entry point → config/logger → ORM models → services → UI (PyQt6).

### Data flow

1. `main.py` initializes logging, creates `QApplication`, loads config singleton, and opens `MainWindow`.
2. `MainWindow` calls `init_db()` (SQLAlchemy `create_all`), creates a `ModService` with a session from `get_session()`, and wires UI signals to service methods.
3. All business operations go through `ModService` which takes a SQLAlchemy `Session` in its constructor — it handles parsing, duplicate detection, file copy/delete, DB writes, and operation logging in one transaction.
4. `ManifestService` is stateless: `parse_from_zipmod(path)` reads `manifest.xml` from inside the zipmod, `check_duplicate()` queries DB for GUID+location conflicts with semantic version comparison via `packaging.version.Version`.
5. `CardService` uses the third-party `kkloader` library to parse Koikatsu character cards and custom binary parsing for clothing cards. `check_mod_availability()` cross-references extracted GUIDs against the database.

### Key architectural rules

- **`.zipmod` files are never unzipped.** Installing a mod = copying the `.zipmod` file into `{game_path}/{AuthorName}/`. Removing = deleting the file.
- **Enable/disable is a rename operation**: `.zipmod` ↔ `.zipmod_disabled` (applies only to GAME-location mods).
- **Same GUID can have two rows** in the `mods` table — one with `location=GAME`, one with `location=REPO`. The composite unique index is `(location, guid)`.
- **Author directory matching is case-insensitive**: `get_author_dir()` searches existing directories case-insensitively before creating new ones.
- **Version comparison** uses `packaging.version.Version`; unparseable versions fall through to `UNKNOWN_VERSION` (conservative — asks user).

### SQLAlchemy sessions

Sessions are created per-operation via `get_session()` (not per-request — there's no web framework). `ModService` receives a session and owns committing/rolling back. The `MainWindow` holds one `ModService` instance for its lifetime. Sessions use SQLite with `check_same_thread=False` for multi-threaded access during batch operations.

### Config

`AppConfig` is a Pydantic model persisted as JSON at `data/config.json` (auto-located next to the exe when packaged, or at project root in dev). Access via the singleton `get_config()` / `reload_config()`.

### Background threads in UI

`BatchInstallDialog` spawns a `QThread` + worker object to run `batch_install()` without blocking the UI. Progress is communicated via Qt signals.

### Key third-party dependency

`kkloader` (`KoikatuCharaData`) is a library for reading/writing Koikatsu character card PNGs. It handles the msgpack-serialized data embedded after the PNG IEND chunk. Used in `CardService.parse_character_card()` and `CardService.kks_to_kk()`.
