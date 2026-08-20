# Store Checklist / Pinel project context

## Project identity

- This repository contains the **Store Checklist** Django application for Pinel and its warranty integration with `pinel.ru`.
- The Django application and the Pinel Bitrix site are hosted on two different servers and use different SSH users, keys, and deployment paths.
- The Bitrix module ID is `pinel.warrantysync` and its PHP namespace is `Pinel\WarrantySync`.

## Connection rules

- Before any remote connection or deployment, confirm that the local working directory is `/Users/bud/Projects/store-checklist`.
- Deploy the Django application to Beget. Use the Pinel Bitrix server only for work on the `pinel.warrantysync` module and its endpoint.
- Always pass `BatchMode=yes`, `IdentitiesOnly=yes`, and `ConnectTimeout=30` to SSH and to the SSH transport used by `rsync`.
- Verify the SSH user and remote working directory before running migrations, changing files, or restarting the application.
- The Beget application key and the Pinel Bitrix key are deliberately separate. Always select the key explicitly with `-i`; do not rely on an SSH agent or automatic key selection.

## Store Checklist production on Beget

- SSH host: `autobud.beget.tech`.
- SSH user: `autobud_checklist`.
- SSH private key path: `/Users/bud/.ssh/id_ed25519_checklist_codex`.
- Django project directory: `/home/a/autobud/checklist/public_html/django_app`.
- Virtualenv: `/home/a/autobud/checklist/public_html/django_venv`.
- Passenger restart file: `/home/a/autobud/checklist/public_html/tmp/restart.txt`.
- Production URL: `https://checklist.es-helper.ru/`.

Connection template:

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=30 \
  -i /Users/bud/.ssh/id_ed25519_checklist_codex \
  autobud_checklist@autobud.beget.tech
```

Deploy files with `rsync` over the same explicit SSH configuration. Upload only the intended tracked files and preserve the server-managed `.env`, `media/`, virtualenv, logs, backups, and Passenger configuration. Example for one file:

```bash
rsync -az \
  -e 'ssh -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=30 -i /Users/bud/.ssh/id_ed25519_checklist_codex' \
  templates/warranty/claim_list.html \
  autobud_checklist@autobud.beget.tech:/home/a/autobud/checklist/public_html/django_app/templates/warranty/claim_list.html
```

After deployment, run the required checks and restart Passenger:

```bash
cd /home/a/autobud/checklist/public_html/django_app
source /home/a/autobud/checklist/public_html/django_venv/bin/activate
python manage.py migrate
python manage.py check
touch /home/a/autobud/checklist/public_html/tmp/restart.txt
```

### Beget SSH connection behavior

- Beget may time out or close newly created SSH connections, especially when several `ssh` or `rsync` sessions are opened close together.
- Avoid parallel SSH connections. Prefer one sequential deployment flow and reuse a successful connection where practical.
- If a connection times out or is closed, do not assume that `rsync` completed. Check its exit status, wait briefly, reconnect with the same explicit key and options, and retry only the incomplete operation.
- After a retry, verify the remote file or run the relevant application check before restarting Passenger.
- A connection interruption is not a reason to change the SSH key, user, or host. Keep using the dedicated Store Checklist credentials above.

## Pinel Bitrix server

- Product: self-hosted «1С-Битрикс: Управление сайтом», not Bitrix24.
- SSH host: `hosting.autobud.ru`.
- SSH user: `pinel_ru_usr`.
- SSH private key path: `/Users/bud/.ssh/id_ed25519_fastpanel_codex`.
- Always use `BatchMode=yes`, `IdentitiesOnly=yes`, and `ConnectTimeout=30`.
- Bitrix document root: `/var/www/pinel_ru_usr/data/www/pinel.ru`.
- Warranty UI: `https://pinel.ru/personal/warranty-claims-list/`.
- Warranty data tables: `warranty` and `warranty_log`.
- Local Bitrix modules directory: `/var/www/pinel_ru_usr/data/www/pinel.ru/local/modules`.

Connection template (contains no secret material):

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=30 \
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
