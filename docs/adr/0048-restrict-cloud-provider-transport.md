---
status: accepted
date: 2026-11-08
decision-makers: operator
---
# Require final HTTPS destinations for cloud providers

Cloud requests carry credentials and classified payloads. Check the configured host with the transport's URL parser.
Require HTTPS, except HTTP to explicitly allowlisted literal 127.0.0.1 and ::1 for local providers and tests.
Reject URL credentials, queries and fragments. Refuse every provider 3xx before the response body is consumed.
Revalidating a second destination would expand the configured trust boundary. Operators must enter the final URL.
Validate both adapters, streaming, embeddings and the configuration boundary with synthetic loopback tests.

Proposed on 2026-11-06, accepted by the operator on 2026-11-08 with router 0.18.2 (release v0.25.2) in operation.
