#!/usr/bin/with-contenv bashio

# Home Assistant passes add-on config as /data/options.json
# Our Python code reads it from there on non-Windows systems
bashio::log.info "Starting Aquarea2MQTT..."
bashio::log.info "MQTT server: $(bashio::config 'MqttServer')"

cd /app
exec python3 main.py
