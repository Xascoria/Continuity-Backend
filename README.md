# 20Seconds server

Run the stack on docker:

    powershell -ExecutionPolicy Bypass -File .\run.ps1

Tests (from this directory, with the dev requirements installed):

    python -m unittest backend.test_api backend.test_migrations backend.test_word_scanner

## Docs

- `backend_design.md`: game rules and the user API (`/api/user/*`).
- `database_schema.md`: tables and indexes.

## Original image (iteration 0)

`image/original.txt` holds the 480x384 original as palette symbols. Regenerate
it from the source PNG with `python tools/make_original.py` (needs Pillow).
