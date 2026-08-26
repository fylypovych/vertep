# Deployment Wizard: technical and security audit

Date: 2026-08-24

## Executive conclusion

The current code is a useful **prototype of the control plane**, not yet a production-ready
zero-shell deployment system. Role metadata, enrollment endpoints, a first-run screen, and
capability matching exist, but selecting a role does not reconcile the running containers and
the issued TLS identity is not used by the worker. The bootstrap and update trust chains also
need hardening before public distribution.

## Remediation status

The first remediation pass now verifies a canonical bootstrap manifest with the pinned release
key, rejects unsafe archive members before extraction, protects first-run with a bootstrap-only
hashed setup code, bounds enrollment payloads, intersects capabilities with the selected role,
adds expiring, audience-bound, generation-bound JWTs plus node revocation, and replaces exported
private keys with node-generated CSR certificates enforced by mTLS on machine routes. The remaining
items below (especially encrypted secrets, Docker-socket isolation,
and release-key lifecycle) remain release blockers. Appliance deployments now use PostgreSQL
and atomically consume registration tokens with `DELETE ... RETURNING`; the JSON backend remains
only as a single-process development fallback.
Bootstrap now downloads the signed role catalog, creates a hashed immutable deployment plan, and
starts only that role's explicit service allowlist. Core plans exclude Worker; GPU plans exclude
Core, PostgreSQL, Redis, Ollama, and Monitoring. The setup UI cannot change a bootstrap-locked role.
Machine requests are also bound to the currently registered certificate serial, so renewal or
revocation immediately invalidates an older client certificate at the Core authorization layer;
proxy-level CRL/OCSP remains required to reject it before forwarding.

## P0 — release blockers

1. **The release-key lifecycle is implemented at the client validation layer but still needs release
   operations.** The Update Agent validates threshold-signed offline-root metadata, metadata rollback
   and expiry, online-key digest/channel scope/revocation, plus manifest validity and monotonic sequence.
   Bootstrap distribution, root-key ceremony/escrow, threshold rotation drills and an end-to-end
   compromised-key recovery test remain required before release.
2. **The container update agent no longer mounts the Docker socket.** It is read-only, capability-free
   and uses `no-new-privileges`. Complete image reconciliation through a narrowly scoped host service
   with signed-plan enforcement and an audit record for every privileged operation.
3. **Bootstrap environment secrets still need sealing.** Core records are now migrated from
   plaintext JSON into a versioned AES-256-GCM envelope with a random nonce and a separate
   installation data key; authenticated reads fail closed after tampering. The data key is now
   sealed with a scrypt-derived KEK and AES-GCM when `SECRET_STORE_PASSPHRASE` is configured, and
   hardened mode fails closed without it. The bootstrap `.env` remains a mode-`0600` host file;
   TPM/KMS sealing, Docker secrets, redaction and online key rotation remain open.
4. **Update recovery is not yet HA-complete.** A local process lease fences concurrent agents,
   audit events are hash-chained, backups are checksum-verified, and Core rollback restores the
   pre-update PostgreSQL dump. Release payloads are now installed in immutable version directories;
   an atomic `current` symlink selects the active release and rollback switches that pointer before
   restoring the database. Multiple Core replicas still require a database-backed fencing epoch;
   rolling/canary coordination and phase-by-phase power-loss tests remain open. Manifests now
   declare database schema/strategy/rollback safety, rolling mode rejects contract migrations,
   and retention pruning preserves both active and rollback releases.

## P1 — correctness and security before beta

1. **Certificate revocation needs proxy-level enforcement.** Enrollment and automatic renewal use
   node-generated keys, CSR, URI SAN, clientAuth EKU, tracked serials, rotated JWT/secret generations,
   and proxy-enforced mTLS. Core rejects revoked identities, but Nginx still needs CRL/OCSP-style
   serial rejection before forwarding a revoked certificate.
2. **Role self-tests need production qualification.** Worker now runs periodic GPU workflow,
   Ollama inference, runtime HTTP, Prometheus, and backup read/write checks, and appliance dispatch
   requires a fresh role-matched pass. Tested capabilities are persisted with heartbeat state and
   intersected with the role allowlist. Real model/module attestations, publisher assertions, and
   physical-GPU failure/recovery tests are still required.

## P2 — architecture and operations

1. Split the current monolithic/minified Web files into versioned JS/CSS modules with accessible
   form controls, structured error states, localization, and browser tests.
2. Expose registered nodes (not only ephemeral heartbeat rows) in Workers UI, including role,
   capabilities, version, latency, disk/RAM/GPU health, certificate expiry, update state, last
   self-test, revoke, drain, and rotate actions.
3. Model worker states as one enum (`READY/FREE`, `BUSY`, `UPDATING`, `OFFLINE`, `ERROR`) and define
   legal transitions. The current code mixes `ONLINE`, `FREE`, and `READY`.
4. Store role catalog versions in the installation manifest. Updates that change a role must include
   a migration/reconciliation plan and compatibility constraints for Core and Worker protocols.
5. Add per-role Compose/image integration tests on Ubuntu 24.04 for `amd64` and `arm64`, with NVIDIA
   tests on real hardware. Test fresh install, reboot, NAT enrollment, token replay, certificate
   renewal, upgrade, interrupted upgrade, and rollback.

## Recommended delivery sequence

1. **Trust chain:** signed bootstrap manifest, documented key rotation, hardened extraction.
2. **Real role reconciliation:** bootstrap controller + generated immutable role plan + profiles.
3. **Enrollment v2:** PostgreSQL token transaction, CSR/mTLS, expiring/revocable credentials,
   pinned Core identity.
4. **Capability attestation:** allowlists, role-specific self-tests, health-aware scheduling.
5. **Secret store:** encrypted-at-rest records are implemented; add key sealing, Docker secrets,
   backup redaction, and key/credential rotation.
6. **Operational UI:** node inventory/actions, backup/restore, health, certificates, logs, updates.
7. **Release qualification:** disposable-VM and physical-GPU end-to-end matrix before calling the
   workflow production-ready.

## Acceptance gates

- A role test proves no forbidden service/image/package is installed for that role.
- Replaying an enrollment token fails under concurrent requests to different Core replicas.
- A revoked node cannot call heartbeat, claim, result, log, or update endpoints.
- Node routes reject JWT-only connections when mTLS is required.
- Tampered bootstrap manifest, update manifest, archive path, symlink, and cross-origin redirect tests fail closed.
- Power loss at every update phase returns either the old or new complete immutable release.
- Secrets do not appear in API responses, process arguments, Compose output, logs, backups, or diagnostics.
- Dispatcher selects only nodes with an approved capability and a current successful self-test.
