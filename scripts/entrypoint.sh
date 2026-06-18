#!/bin/bash
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Create group/user matching the requested UID:GID if they don't already exist
if ! getent group "${PGID}" > /dev/null 2>&1; then
    groupadd -g "${PGID}" gameserver
fi
if ! getent passwd "${PUID}" > /dev/null 2>&1; then
    useradd -u "${PUID}" -g "${PGID}" -M -s /bin/bash gameserver
fi

# Ensure volume mount points exist and are owned by the service user
mkdir -p "${SERVERFILES_PATH:-/serverfiles}" \
         "${CONFIG_DIR:-/config}" \
         "${GAMEDATA_PATH:-/gamedata}"
chown -R "${PUID}:${PGID}" \
    "${SERVERFILES_PATH:-/serverfiles}" \
    "${CONFIG_DIR:-/config}" \
    "${GAMEDATA_PATH:-/gamedata}" \
    /opt/steamcmd \
    /opt/panel

# Drop privileges and exec Flask as the service user
exec gosu "${PUID}:${PGID}" /opt/panel/bin/python /opt/panel/app/app.py
