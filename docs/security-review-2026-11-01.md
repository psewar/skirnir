# Skirnir Security Review — 2026-11-01

**Scope:** static audit of the public `psewar/skirnir` repository at commit `054ff09f34a806d03883ddd21b8c9755a264ced4`; reviewed the aiohttp router, Go agent, session and memory boundaries, systemd units, dependencies and CI.

**Limitations:** no production configuration access, no runtime penetration test, no complete historical secret scan. Risk ratings depend on network exposure and active features.

## Findings and remediation tracking

| ID | Severity | Finding | Mitigation in this PR |
| --- | --- | --- | --- |
| SK-SEC-01 | High | API `enforce` with empty client list allowed unauthenticated inference; empty control user map allowed administrative access | Deny API when `enforce` is set regardless of client count; deny missing control auth on network bindings, retain loopback-only developer exception |
| SK-SEC-02 | High | Unapproved Ed25519 enrollment lacked an admission cap; excessive handshake/body sizes and pending keys exhausted memory/disk | 32 KiB handshake, 2 MiB subsequent frames, max 32 pending, per-IP enrollment admission bound |
| SK-SEC-03 | High when cloud enabled | Request `routing.data_class` could override a stricter server-side client class | Effective classification is at least the configured client/server minimum |
| SK-SEC-04 | Medium | State-changing Control endpoints accepted browser Basic Auth without explicit anti-CSRF controls | Non-simple UI header on mutating admin requests and Fetch Metadata cross-site rejection |
| SK-SEC-05 | Medium | Default client access to concrete models bypassed role-only grants | **Open:** the permissive historical default is preserved for compatibility; a staged transition to role-only access is still needed |
| SK-SEC-06 | Medium | Example credentials were publicly known | Replace fixed example admin password hashes with a non-known random hash; operators must configure their own |
| SK-SEC-07 | Medium | Router and Linux agent run as root | Add kernel/capability and privilege escalation restrictions while retaining required updater permissions; full unprivileged service transition remains follow-up |
| SK-SEC-08 | Medium | Dependencies include older Python/Go packages and security scan gaps | Upgrade Go Paho MQTT and x/net modules with CI-generated go.sum, add pip-audit, govulncheck CI and Dependabot monitoring |

## Review notes

### Isolation and existing strengths
The session API binds ownership to authenticated token clients; memory access checks principal/project grants and per-bank HMAC identity. Parameterized PostgreSQL queries avoid straightforward SQL injection. New agents prove possession of an Ed25519 identity key and require approval. Agent-update manifests are signed, and password/token handling is generally stronger than default Ollama deployments.

### Remaining risk and operational migration
- **Production config:** set `router.client_auth.mode: enforce`, `locked: true`, strong tokens and PBKDF2 admin passwords; use TLS and restrictive firewall rules.
- **Control API clients:** mutating `/admin/*` requests must send `X-Skirnir-UI: 1`; legitimate browser UI adds it automatically. CLI clients must be updated.
- **Agent enrollment:** rate limits apply only to new keys; approved identities reconnect without enrollment limits. Replace pending abandoned keys through the administrative UI.
- **Model access:** set `models: false` for role-only clients; a future stricter default requires a migration.
- **Cloud:** configure client minimum classes and `cloud: false` where external forwarding must never happen; sensitive data checks are defense in depth, not a substitute.
- **Dependencies:** the installed runtime may not match dependency minimums. Supply-chain CVEs require a lockfile-resolved build and `govulncheck`; until then dependency risk remains open.
- **Systemd:** root operation is still present due to self-update/hardware requirements; dedicated service users and capability policy need staging tests.

### Future validation
Run CI and security regressions, review expected client behavior, verify host-bound port restrictions, exercise real WebSocket enrollment and agent reconnect, inspect cross-origin browser behavior, and complete a controlled runtime penetration test.

**Publication/privacy:** This report contains no private hostnames, secrets, IP addresses, or other user-specific infrastructure details. It deliberately omits deployment topology.
