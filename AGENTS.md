# Store Checklist / Pinel project context

## Project identity

- This repository and its warranty integration belong to **Pinel** (`pinel.ru`).
- Do not mix in naming, hosts, SSH aliases, paths, branding, module IDs, or deployment instructions from any other project.
- The Bitrix module ID is `pinel.warrantysync` and its PHP namespace is `Pinel\WarrantySync`.

## Pinel Bitrix server

- Product: self-hosted «1С-Битрикс: Управление сайтом», not Bitrix24.
- SSH host: `hosting.autobud.ru`.
- SSH user: `pinel_ru_usr`.
- SSH private key path: `/Users/bud/.ssh/id_ed25519_fastpanel_codex`.
- Always use `BatchMode=yes` and `IdentitiesOnly=yes`.
- Bitrix document root: `/var/www/pinel_ru_usr/data/www/pinel.ru`.
- Warranty UI: `https://pinel.ru/personal/warranty-claims-list/`.
- Warranty data tables: `warranty` and `warranty_log`.
- Local Bitrix modules directory: `/var/www/pinel_ru_usr/data/www/pinel.ru/local/modules`.

Connection template (contains no secret material):

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes \
  -i /Users/bud/.ssh/id_ed25519_fastpanel_codex \
  pinel_ru_usr@hosting.autobud.ru
```

## Warranty synchronization module

- Local source: `bitrix-module/pinel.warrantysync`.
- Local distributable: `bitrix-module/pinel.warrantysync.zip`.
- Server module directory: `/var/www/pinel_ru_usr/data/www/pinel.ru/local/modules/pinel.warrantysync`.
- Server archive: `/var/www/pinel_ru_usr/data/www/pinel.ru/local/modules/pinel.warrantysync.zip`.
- Public endpoint after installation: `https://pinel.ru/warranty-sync/`.
- Installed and enabled in Bitrix on 2026-08-20.
- The shared HMAC secret is configured in the Bitrix module options and the local ignored `.env`; its value must never be documented or committed.
- Signed connectivity was verified from the Django `BitrixWarrantyClient` with `health` and read-only `claims.list`; module version `1.0.0` responded successfully.

## Security and deployment rules

- Never commit, print, or copy the SSH private key or HMAC synchronization secret into the repository.
- Before overwriting an existing server module, inspect it and create a timestamped backup.
- Do not write directly to Bitrix warranty tables from Django. Use the installed signed module API.
- The API may update only explicitly allowlisted fields; currently `UF_STATUS` and `UF_COMMENT`.
- Validate PHP syntax on the Pinel server before running the module installer or an update.

## GitHub workflow

- After completing changes to code, configuration, migrations, or documentation, verify them, commit them, and push the current branch to GitHub unless the user explicitly asks not to.
- Never commit secrets, local databases, user uploads, or temporary/generated runtime files.
- Report the pushed branch and commit ID in the final response.

## External object IDs

- Persist identifiers returned by external services immediately after successful creation.
- For Telegram warranty discussions, always store both `message_thread_id` and every known `message_id` so topics and messages can be managed later.
