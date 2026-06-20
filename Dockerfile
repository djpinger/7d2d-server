FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8

# 32-bit libs required by SteamCMD
RUN dpkg --add-architecture i386 && \
    apt-get update && apt-get install -y \
        python3 python3-pip python3-venv \
        lib32gcc-s1 \
        curl \
        gosu \
        locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# SteamCMD — panel handles game install/update
RUN mkdir -p /opt/steamcmd && \
    curl -sqL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" | \
    tar zxvf - -C /opt/steamcmd

# Python venv for the panel
RUN python3 -m venv /opt/panel
COPY panel/requirements.txt /opt/panel/requirements.txt
RUN /opt/panel/bin/pip install --no-cache-dir -r /opt/panel/requirements.txt

# Panel application
COPY panel/ /opt/panel/app/

# Entrypoint (runs as root, drops to PUID:PGID at runtime)
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /serverfiles /config /gamedata /logs

EXPOSE 8090

ENTRYPOINT ["/entrypoint.sh"]
