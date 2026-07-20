"""PSK Reporter MQTT listener for DX Spotter.

Manages a single Paho MQTT connection to the PSK Reporter broker, parses
incoming JSON payloads, and forwards raw spot dicts to a caller-supplied
callback.

The caller is responsible for building the subscription topic and establishing
raw payloads (DXCC lookup, distance computation, etc.) before passing them to
the Qt spot table.

Example usage::

    listener = MqttListener(
        host='mqtt.pskreporter.info',
        port=1883,
        topic='pskr/filter/v2/20m/FT8/+/+/+/+/+/+/#',
        on_spot=my_callback,
    )
    listener.start()
    # … later …
    listener.stop()
"""
from __future__ import annotations

import json
from typing import Callable

import paho.mqtt.client as mqtt

# Callable receiving a raw PSK Reporter JSON payload dict.
SpotCallback = Callable[[dict], None]


class MqttListener:
    """Manages a Paho MQTT connection to the PSK Reporter broker.

    Connects to the broker, subscribes to a topic, parses incoming JSON
    payloads, and forwards raw spot dicts to ``on_spot``.  Connection state
    is available via the :attr:`connected` property and is polled by the
    caller (rather than pushed via callbacks) to keep the interface simple.

    Parameters
    ----------
    host : str
        MQTT broker hostname or IP address.
    port : int
        MQTT broker TCP port (PSK Reporter uses ``1883``).
    topic : str
        Initial subscription topic string.
    on_spot : SpotCallback
        Called for each successfully parsed JSON payload.  Receives the raw
        dict directly from ``json.loads`` — no filtering or enrichment applied.
        Invoked from the Paho network thread; implementations must be
        thread-safe (use Qt signals, not direct widget updates).
    """

    def __init__(
        self,
        host: str,
        port: int,
        topic: str,
        on_spot: SpotCallback,
    ) -> None:
        """Initialize the listener without connecting.

        Parameters
        ----------
        host : str
            MQTT broker hostname.
        port : int
            MQTT broker port.
        topic : str
            Subscription topic used on first connect.
        on_spot : SpotCallback
            Callback invoked for each parsed spot payload dict.
        """
        self._host = host
        self._port = port
        self._topic = topic
        self._on_spot = on_spot
        self._connected: bool = False

        self._client: mqtt.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # -- public interface -----------------------------------------------------

    @property
    def connected(self) -> bool:
        """``True`` when the broker connection is currently established."""
        return self._connected

    @property
    def topic(self) -> str:
        """The active subscription topic string."""
        return self._topic

    def start(self) -> None:
        """Connect to the broker and start the background network loop.

        Returns immediately; the Paho ``loop_start()`` thread handles I/O.
        The :attr:`connected` property transitions to ``True`` once the broker
        acknowledges the connection (asynchronously).
        """
        print(f"MQTT: connecting to {self._host}:{self._port}")
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        """Stop the network loop and disconnect cleanly from the broker."""
        self._client.loop_stop()
        self._client.disconnect()

    def resubscribe(self, new_topic: str) -> None:
        """Switch the active subscription to a new topic.

        Unsubscribes from the current topic (if any), stores the new topic,
        and subscribes.  Safe to call with the same topic string to force a
        clean resubscription (e.g., after a restart).

        Parameters
        ----------
        new_topic : str
            Replacement subscription topic.
        """
        if self._topic:
            self._client.unsubscribe(self._topic)
        self._topic = new_topic
        self._client.subscribe(self._topic)
        print(f"MQTT: subscribed to {self._topic}")

    # -- paho callbacks (network thread) --------------------------------------

    def _handle_connect(self, client, userdata, flags, rc, properties) -> None:
        # Fired by paho on successful broker connection.
        self._connected = True
        print(f"MQTT: connected (rc={rc}), subscribing to {self._topic}")
        client.subscribe(self._topic)

    def _handle_disconnect(self, client, userdata, flags, rc, properties) -> None:
        # Fired by paho on clean or unclean disconnect.
        self._connected = False
        print(f"MQTT: disconnected (rc={rc})")

    def _handle_message(self, client, userdata, msg) -> None:
        # Parse incoming JSON and forward raw payload to the caller's callback.
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError as exc:
            print(f"MQTT: JSON decode error: {exc}")
            return
        self._on_spot(payload)
