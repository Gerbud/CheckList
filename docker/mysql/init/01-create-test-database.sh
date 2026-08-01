#!/bin/sh
set -eu

case "${MYSQL_TEST_DATABASE}" in
    ''|*[!a-zA-Z0-9_]*)
        echo "MYSQL_TEST_DATABASE must contain only letters, digits, and underscores." >&2
        exit 1
        ;;
esac

case "${MYSQL_USER}" in
    ''|*[!a-zA-Z0-9_]*)
        echo "MYSQL_USER must contain only letters, digits, and underscores." >&2
        exit 1
        ;;
esac

MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" mysql --protocol=socket -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_TEST_DATABASE}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`${MYSQL_TEST_DATABASE}\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL
