#!/bin/sh
(
    set -eu
    exporter_password=$(cat /run/secrets/mysql-exporter)
    case "$exporter_password" in
        ''|*[!0-9a-fA-F]*)
            echo 'Use a hex value for the mysql-exporter secret.' >&2
            exit 1
            ;;
    esac
    if [ "${#exporter_password}" -lt 32 ]; then
        echo 'The mysql-exporter secret needs at least 32 hex characters.' >&2
        exit 1
    fi

    export MYSQL_PWD="$(cat /run/secrets/mysql-root)"
    mysql --protocol=SOCKET --user=root <<SQL
CREATE TABLE matchuri_perf.perf_environment (
    id INT PRIMARY KEY,
    environment VARCHAR(32) NOT NULL
);
INSERT INTO matchuri_perf.perf_environment (id, environment) VALUES (1, 'matchuri-perf');
CREATE USER 'matchuri_metrics'@'%' IDENTIFIED BY '${exporter_password}' WITH MAX_USER_CONNECTIONS 3;
SQL
)
