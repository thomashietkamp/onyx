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

## Users and groups from Azure (Entra ID)

Entra ID provisions users and groups into Onyx over SCIM 2.0.

1. In Onyx, open Admin → Permissions → SCIM and generate a token. Copy it;
   Onyx shows it once. A new token revokes the old one.
2. In the Entra admin center, open Enterprise applications → New application →
   Create your own application (non-gallery), then Provisioning → Automatic.
3. Set Tenant URL to `https://<your-onyx-host>/api/scim/v2` and Secret token to
   the Onyx token. Test the connection.
4. Under Mappings, keep `userPrincipalName` → `userName`, `mail` →
   `emails[type eq "work"].value`, and the group `displayName`. Assign the users
   and groups to the application and start provisioning.

What Onyx does with it:
- A provisioned user is matched to an existing Onyx account by email, or
  created. Deprovisioning deactivates the account and keeps its data.
- A provisioned group becomes an Onyx user group, with the same members. Give
  it connectors (data access) and permissions in Admin → Groups. Managers set
  in Onyx survive the IdP's periodic syncs.
- The seeded Admin and Basic groups belong to Onyx; Entra cannot rename,
  change or delete them.

## LiteLLM Proxy

`fork/litellm/` holds a LiteLLM Proxy that Onyx uses as one gateway to
Requesty, Azure OpenAI and self-hosted models. See `fork/litellm/README.md`.

## Known gaps

- Secret encryption is this fork's own format (AES-256-GCM,
  `backend/onyx/utils/encryption_keyring.py`). It reads the same settings as
  upstream (`ENCRYPTION_KEY_SECRET`, `ENCRYPTION_KEYS`,
  `ENCRYPTION_WRITE_KEY_ID`), but it cannot read ciphertext that upstream EE
  wrote. After a key change, run `python -m scripts.encrypt_stored_secrets`.
  That script covers encrypted database columns only.
- Permission sync (access type "Auto Sync") works for SharePoint, OneDrive,
  Teams, Outlook, Google Drive, Gmail, Confluence, Slack, GitHub, Box, Canvas,
  Egnyte, Linear, YouTrack, Azure Data Lake, Zoom and HubSpot. Jira and
  Salesforce cannot be set to Auto Sync: Jira issue security levels are not
  read, and Salesforce records need post-query censoring. For the Microsoft
  sources, the app registration needs the Graph permissions `Group.Read.All`
  and `GroupMember.Read.All`. The heavy worker runs the syncs, every 30
  minutes for each pair. None of the sources is tested against a live account.
- The fork runs at the Enterprise tier (`backend/onyx/utils/tier.py`). Every
  user keeps the CE right to create agents (`add:agents`), also when their
  groups do not grant it.
- The connector form runs no credential checks for permission sync
  (`onyx/connectors/capability_applicability.py` and `perm_sync_valid.py` do
  not exist). Missing source rights show up as a failed sync attempt instead.
- Not rebuilt, because nothing calls them: `GET /manage/admin/manage-access-prefill`
  and `POST /enterprise-settings/refresh-token`. Cloud-only parts (billing,
  licenses, tenants, connector OAuth, post-query censoring for Salesforce) are
  out of scope.
- Upstream CI workflows still call `scripts.seed_dev_license`.
- The integration suite still holds tests for EE features (user groups,
  connector access per group). They fail until those features exist here.
