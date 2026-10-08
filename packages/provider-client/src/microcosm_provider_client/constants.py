"""Shared transport and lifecycle defaults."""

from typing import Final

MODULE_GROUP: Final = "microcosm.provider.modules"
MODULE_HOST: Final = "microcosm_provider_client"
SOCKET_FILENAME: Final = "service.sock"
RUNTIME_PREFIX: Final = "pe-service-"
ACK_OK: Final = b"ok\n"
ACK_ERROR: Final = b"error\n"
DELIMITER: Final = b"\n"
MAX_MESSAGE_BYTES: Final = 1_048_576
READ_BYTES: Final = 65_536
SEND_TIMEOUT: Final = 0.2
STARTUP_TIMEOUT: Final = 3.0
STARTUP_POLL: Final = 0.02
SOCKET_TIMEOUT: Final = 0.25
TICK_SECONDS: Final = 1.0
DRAIN_SECONDS: Final = 15.0
PROCESS_EXIT_TIMEOUT: Final = 1.0
