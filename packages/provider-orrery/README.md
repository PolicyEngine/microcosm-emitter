# Microcosm provider Orrery publication

Publishes preserved graph and execution-evidence files through the immutable
Microcosm runs API. Publication IDs do not require a telemetry run. Pending jobs
retain their exact inventory, bytes, leases and retry status independently of
telemetry retention. This package neither generates Microcosm graphs nor imports
Microcosm. HTTP delivery runs in the selected `orrery` service module.
