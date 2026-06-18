FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8

# 32-bit support required by the game server
RUN dpkg --add-architecture i386 && \
    apt-get update && apt-get install -y \
        python3 python3-pip python3-venv \
        lib32gcc-s1 \
        curl \
        locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# Install SteamCMD manually (avoids Ubuntu package EULA prompt)
RUN mkdir -p /opt/steamcmd && \
    curl -sqL "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" | \
    tar zxvf - -C /opt/steamcmd && \
    ln -s /opt/steamcmd/steamcmd.sh /usr/local/bin/steamcmd

# Python venv for the panel
RUN python3 -m venv /opt/panel
COPY panel/requirements.txt /opt/panel/requirements.txt
RUN /opt/panel/bin/pip install --no-cache-dir -r /opt/panel/requirements.txt

# Panel application
COPY panel/ /opt/panel/app/

# Startup script
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default volume mount points
RUN mkdir -p /serverfiles /config /gamedata

EXPOSE 8090 26900 26901 26902 8080 8081

ENTRYPOINT ["/entrypoint.sh"]
