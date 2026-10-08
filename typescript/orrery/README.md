# Orrery publication reads

The package owns publication validation, server-side metadata/evidence reads and
browser-side bounded downloads, SHA-256 verification and Orrery parsing.
It does not contain React components, routes, storage credentials or a hosted API.

Import `createPublicationReader` from the `/server` entry point with a storage
adapter. Import `loadDocument` from `/browser` in the runs app. Browser exports
never import Node cryptography or server credentials. Existing publication URLs
and the Orrery React component remain unchanged.

Build and test with Bun. Package publication and consumer activation are separate
operations; do not activate consumers before verifying the published tarball.
