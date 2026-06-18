#!/bin/bash
set -e

mkdir -p "${SERVERFILES_PATH:-/serverfiles}" \
         "${CONFIG_DIR:-/config}" \
         "${GAMEDATA_PATH:-/gamedata}"

exec /opt/panel/bin/python /opt/panel/app/app.py
