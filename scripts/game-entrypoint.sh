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

# Symlink game's default data path to the named volume
mkdir -p "${_home}/.local/share"
ln -sfn "${GAMEDATA_PATH:-/gamedata}" "${_home}/.local/share/7DaysToDie"

# Steam SDK symlink — required for Steamworks GameServer init
mkdir -p "${_home}/.steam/sdk64"
ln -sfn "${SERVERFILES_PATH:-/serverfiles}/steamclient.so" "${_home}/.steam/sdk64/steamclient.so"

# Auto-inject UserDataFolder into config if absent
_config="${CONFIG_PATH:-/config/sdtdserver.xml}"
_gamedata="${GAMEDATA_PATH:-/gamedata}"
if [ -f "$_config" ] && ! grep -q 'UserDataFolder' "$_config"; then
    sed -i "s|<property name=\"AdminFileName\"|<property name=\"UserDataFolder\" value=\"${_gamedata}\" />\n\t<property name=\"AdminFileName\"|" "$_config"
fi

chown -R "${PUID}:${PGID}" \
    "${_home}" \
    "${SERVERFILES_PATH:-/serverfiles}" \
    "${CONFIG_DIR:-/config}" \
    "${GAMEDATA_PATH:-/gamedata}" \
    "${LOG_DIR:-/logs}"

_exe="${SERVERFILES_PATH:-/serverfiles}/7DaysToDieServer.x86_64"
if [ ! -f "$_exe" ]; then
    echo "[7d2d-game] Game server not installed. Use the panel Install / Update button."
    exit 0
fi

cd "${SERVERFILES_PATH:-/serverfiles}"
exec gosu "${PUID}:${PGID}" \
    "$_exe" \
    -logfile "${LOG_DIR:-/logs}/server.log" \
    -quit -batchmode -nographics -dedicated \
    -configfile="${CONFIG_PATH:-/config/sdtdserver.xml}"
