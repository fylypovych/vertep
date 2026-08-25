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

## P0 — release blockers

1. **The shipped update public key has no documented release-key lifecycle.** Define separate
   offline root and online release keys, key IDs, validity windows, revocation, and an offline
   root-signed key rotation document. Do not publish a generated placeholder as a production
   trust anchor.
2. **The update agent mounts the unrestricted Docker socket.** A compromised agent becomes root
   on the host. Put updates behind a narrowly scoped host service or a socket proxy that permits
   only required image/compose operations, run it with a signed deployment plan, and audit every
   operation.
3. **Secrets are plaintext JSON and `.env` files.** Mode `0600` is necessary but does not meet the
   stated encrypted secret-store requirement. Encrypt records with an installation data key
   sealed by TPM 2.0 or a passphrase/KMS, use per-record nonces and authenticated encryption,
   redact backup/log output, and support rotation without reinstalling nodes.

## P1 — correctness and security before beta

1. **Certificate revocation needs proxy-level enforcement.** Enrollment and automatic renewal use
   node-generated keys, CSR, URI SAN, clientAuth EKU, tracked serials, rotated JWT/secret generations,
   and proxy-enforced mTLS. Core rejects revoked identities, but Nginx still needs CRL/OCSP-style
   serial rejection before forwarding a revoked certificate.
2. **Role self-tests need production qualification.** Worker now runs periodic GPU workflow,
   Ollama inference, runtime HTTP, Prometheus, and backup read/write checks, and appliance dispatch
   requires a pass. Real model fixtures, durable attestation, publisher assertions, and physical-GPU
   failure/recovery tests are still required.

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
5. **Secret store:** encrypted-at-rest records, backup redaction, key/credential rotation.
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
