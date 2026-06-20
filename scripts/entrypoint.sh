#!/bin/bash
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if ! getent group "${PGID}" > /dev/null 2>&1; then
    groupadd -g "${PGID}" gameserver
fi
if ! getent passwd "${PUID}" > /dev/null 2>&1; then
    useradd -u "${PUID}" -g "${PGID}" -d /home/gameserver -m -s /bin/bash gameserver
fi

_home=$(getent passwd "${PUID}" | cut -d: -f6)
mkdir -p "${_home}" \
         "${SERVERFILES_PATH:-/serverfiles}" \
         "${CONFIG_DIR:-/config}" \
         "${GAMEDATA_PATH:-/gamedata}" \
         "${LOG_DIR:-/logs}"

# Allow the service user to access the Docker socket (needed to manage the game container).
if [ -S /var/run/docker.sock ]; then
    chmod 666 /var/run/docker.sock
fi

chown -R "${PUID}:${PGID}" \
    "${_home}" \
    "${SERVERFILES_PATH:-/serverfiles}" \
    "${CONFIG_DIR:-/config}" \
    "${GAMEDATA_PATH:-/gamedata}" \
    "${LOG_DIR:-/logs}" \
    /opt/steamcmd \
    /opt/panel

exec gosu "${PUID}:${PGID}" /opt/panel/bin/python /opt/panel/app/app.py
