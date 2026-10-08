# PolicyEngine local service

A per-build subprocess host for Linux and macOS, using a private Unix socket.

Applications explicitly select an installed module through the `policyengine.local_service.modules` entry-point group. The runtime owns startup, message acknowledgement, parent-process monitoring, and bounded shutdown. Modules own their domain behavior and persistence.

This distribution does not depend on telemetry, authentication, databases, or Microcosm. It installs no system daemon and opens no TCP listener.
