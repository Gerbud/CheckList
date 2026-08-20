#!/usr/bin/env bash

# Единственная точка входа для всех периодических задач Store Checklist на Beget.
# Beget запускает этот файл каждую минуту; новые задачи добавляются сюда.

set -u

readonly APP_DIR='/home/a/autobud/checklist/public_html/django_app'
readonly PYTHON='/home/a/autobud/checklist/public_html/django_venv/bin/python'
readonly RUNTIME_DIR='/home/a/autobud/checklist/public_html/tmp'
readonly LOCK_FILE="${RUNTIME_DIR}/store_checklist_cron.lock"
readonly DAILY_STAMP_FILE="${RUNTIME_DIR}/greenworks_drawings_date"

cd "${APP_DIR}" || exit 1

exec 9>"${LOCK_FILE}"
if ! /usr/bin/flock -n 9; then
    printf '%s cron: previous run is still active, skipping\n' "$(date --iso-8601=seconds)"
    exit 0
fi

exit_code=0

run_command() {
    local label="$1"
    shift
    printf '%s cron: start %s\n' "$(date --iso-8601=seconds)" "${label}"
    if "${PYTHON}" manage.py "$@"; then
        printf '%s cron: done %s\n' "$(date --iso-8601=seconds)" "${label}"
        return 0
    else
        local status=$?
        printf '%s cron: failed %s (exit %s)\n' "$(date --iso-8601=seconds)" "${label}" "${status}" >&2
        exit_code=1
        return "${status}"
    fi
}

run_command 'telegram inbound queue' process_telegram_inbound_queue --limit 50
run_command 'telegram notification scheduler' schedule_telegram_notifications
run_command 'telegram outbound queue' process_telegram_queue --limit 50
run_command 'Bitrix warranty synchronization' sync_bitrix_warranty --limit 500
run_command 'warranty Telegram topics' sync_warranty_telegram --limit 200

today="$(date +%F)"
last_daily_run=''
if [[ -f "${DAILY_STAMP_FILE}" ]]; then
    read -r last_daily_run < "${DAILY_STAMP_FILE}" || true
fi
if [[ "${last_daily_run}" != "${today}" ]]; then
    if run_command 'Greenworks drawings refresh' refresh_greenworks_drawings; then
        printf '%s\n' "${today}" > "${DAILY_STAMP_FILE}"
    fi
fi

exit "${exit_code}"
