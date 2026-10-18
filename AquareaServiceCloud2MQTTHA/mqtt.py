"""
MQTT handler — equivalent of mqtt.go
Requires: pip install aiomqtt
"""

import asyncio
import logging
import aiomqtt
from aquarea_types import AquareaCommand

logger = logging.getLogger(__name__)

SUBSCRIBE_TOPIC = "aquarea/+/settings/+/set"
STATUS_TOPIC = "aquarea/status"

RECONNECT_INTERVAL = 5  # seconds between reconnection attempts


async def mqtt_handler(
    ctx: asyncio.Event,
    config: dict,
    data_queue: asyncio.Queue,
    command_queue: asyncio.Queue,
    status_queue: asyncio.Queue,
):
    logger.info("Starting MQTT handler")

    keepalive = int(float(config.get("MqttKeepalive", 60)))
    attempt = 0

    while not ctx.is_set():
        try:
            if attempt > 0:
                logger.info("MQTT attempting reconnection #%d...", attempt)
            async with aiomqtt.Client(
                hostname=config["MqttServer"],
                port=config.get("MqttPort", 1883),
                username=config.get("MqttLogin") or None,
                password=config.get("MqttPass") or None,
                identifier=config.get("MqttClientID", "aquarea"),
                keepalive=keepalive,
                clean_session=True,
                will=aiomqtt.Will(topic=STATUS_TOPIC, payload="offline", qos=0, retain=True),
            ) as client:
                if attempt == 0:
                    logger.info("MQTT connected to %s:%s", config["MqttServer"], config.get("MqttPort", 1883))
                else:
                    logger.info("MQTT reconnected to %s:%s (after %d attempt(s))", config["MqttServer"], config.get("MqttPort", 1883), attempt)
                attempt = 0
                await client.subscribe(SUBSCRIBE_TOPIC, qos=2)
                await client.publish(STATUS_TOPIC, "online", qos=0, retain=True)

                async def read_incoming():
                    async for msg in client.messages:
                        parts = str(msg.topic).split("/")
                        if len(parts) > 3:
                            device_id = parts[1]
                            setting = parts[3]
                            value = msg.payload.decode()
                            logger.info("Command received: device=%s setting=%s value=%s", device_id, setting, value)
                            await command_queue.put(
                                AquareaCommand(device_id=device_id, setting=setting, value=value)
                            )

                async def dispatch_outgoing():
                    while not ctx.is_set():
                        # Drain data queue
                        try:
                            while True:
                                data = data_queue.get_nowait()

                                if data is None:
                                    logger.warning("Received None in data queue, skipping")
                                    continue

                                if not isinstance(data, dict):
                                    logger.error("Invalid data format in queue: %s", type(data))
                                    continue

                                for key, value in data.items():
                                    await client.publish(key, value, qos=0, retain=True)

                        except asyncio.QueueEmpty:
                            pass

                        # Drain status queue
                        try:
                            while True:
                                online: bool = status_queue.get_nowait()
                                status = "online" if online else "offline"
                                await client.publish(STATUS_TOPIC, status, qos=0, retain=True)
                        except asyncio.QueueEmpty:
                            pass

                        await asyncio.sleep(0.01)

                    await client.publish(STATUS_TOPIC, "offline", qos=0, retain=True)

                # asyncio.gather() does NOT cancel the sibling when one side
                # fails: the survivor would keep running against a dead client
                # (orphan task). Cancel it explicitly, then re-raise.
                tasks = [
                    asyncio.create_task(read_incoming()),
                    asyncio.create_task(dispatch_outgoing()),
                ]
                try:
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_EXCEPTION
                    )
                finally:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                for t in done:
                    if not t.cancelled() and t.exception():
                        raise t.exception()

        except aiomqtt.MqttError as e:
            if ctx.is_set():
                break
            attempt += 1
            logger.warning("MQTT connection lost (%s), reconnecting in %ds...", e, RECONNECT_INTERVAL)
            await asyncio.sleep(RECONNECT_INTERVAL)
        except Exception as e:
            if ctx.is_set():
                break
            attempt += 1
            logger.error("Unexpected MQTT error (%s), reconnecting in %ds...", e, RECONNECT_INTERVAL)
            await asyncio.sleep(RECONNECT_INTERVAL)

    logger.info("MQTT handler stopped")