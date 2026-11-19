# MIT-only fork

This fork contains only MIT-licensed code. It has no Enterprise Edition (EE)
code, so it needs no Onyx license. Features that upstream ships in EE are
rebuilt here as new MIT code.

## Rules

- Do not copy, port or paraphrase code from upstream `backend/ee/`,
  `web/src/ee/` or `web/src/app/ee/`. Write new code from the MIT code, the
  public docs and the API contracts that the MIT frontend already uses.
- Put rebuilt features in the MIT tree (`backend/onyx/`, `web/src/`), never
  under an `ee/` directory.
- Upstream EE code wins over MIT code when both exist
  (`onyx/utils/variable_functionality.py:fetch_versioned_implementation`).
  This fork has an empty `backend/ee` package, so the MIT code always runs.

## Sync with upstream

```bash
git fetch upstream main
git merge upstream/main          # conflicts on EE paths are expected
./fork/strip_ee.sh               # removes EE code again, resolves those conflicts
```

`strip_ee.sh` stops with a list of MIT files that still reference EE code.
Fix each one by hand, then run the script again until it reports
"no EE code left". Then run the backend unit tests and the web typecheck.

Tests that use the `enable_ee` fixture check EE behavior. They skip in this
build (`backend/tests/conftest.py:_ee_enabled`).

## Known gaps

- Secret encryption is this fork's own format (AES-256-GCM,
  `backend/onyx/utils/encryption_keyring.py`). It reads the same settings as
  upstream (`ENCRYPTION_KEY_SECRET`, `ENCRYPTION_KEYS`,
  `ENCRYPTION_WRITE_KEY_ID`), but it cannot read ciphertext that upstream EE
  wrote. After a key change, run `python -m scripts.encrypt_stored_secrets`.
  That script covers encrypted database columns only.
- Search mode (`web/src/sections/search/SearchUI.tsx`) and document search in
  the agent knowledge pane (`web/src/lib/search/documentSearch.ts`) are
  placeholders.
- `web/src/proxy.ts:EE_ROUTES` still rewrites EE admin paths to `/ee/...`,
  which now returns 404. Each path goes away when its feature is rebuilt.
- Upstream CI workflows still call `scripts.seed_dev_license`.
- The integration suite still holds tests for EE features (user groups,
  connector access per group). They fail until those features exist here.
