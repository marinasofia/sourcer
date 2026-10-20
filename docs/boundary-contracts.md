# Boundary contracts

HTTP and MCP call `sourcer.pipeline.scoring.score_text`. It validates a nonempty string
posting of at most 8,000 characters, string title/company fields, deterministic
exclusions, and estimated budget admission. HTTP omits the parsed object from
its response to preserve its existing response shape. HTTP malformed requests
return 400, oversized bodies 413, provider failures 502, and internal failures
500 with a generic message. Model parsing or budget failures remain error objects
in a 200 response for compatibility. MCP propagates input validation errors.

`Usage.can_call` uses unrounded estimated spend before each application-level
model request and corrective retry. Zero, negative, or nonfinite caps disable
calls. Invalid prices also disable admission. An admitted call can exceed the
estimate, SDK transport retries are separate, and the HTTP and MCP servers
each keep one usage counter for their whole life, so the cap bounds their total
spend. Provider limits remain the hard ceiling. The bundled HTTP server needs a
bearer token on every scoring and digest request, binds loopback by default,
runs at most SOURCER_HTTP_CONCURRENCY scores at once, and bounds body size,
connections, and the time a connection may take. The Kubernetes example binds
all container interfaces behind a ClusterIP service.

Model scores must be finite integers from 0 through 100. Experience must be
finite and nonnegative; boolean numbers, invalid enums, and non-string list
members are rejected. Unknown keys are dropped. Truncated or refused responses
are not accepted. Schema errors permit one corrective retry subject to budget.
No regression test makes a paid model call.

Restore treats the posting hash as identity. New hashes retain tracking and
verification metadata; existing hashes preserve the current local state.
Generated database IDs can change. Run-log insertion and posting insertion do
not yet form one atomic snapshot operation.

The evaluator loads records and their bundled profile together. An explicit
profile argument wins; a bare list or fixture without a profile falls back to
configured policy. Operator settings cannot silently change the shipped fixture.

Submission authorization is private database state. Company input cannot set
`submit_enabled`; any attempt fails before seeding starts. Existing authorization
is preserved only while a company remains in the configured inventory. Removing
a company disables its row.

Open: transactional recovery and signed release artifacts. The HTTP service
has bearer authentication, a process-wide budget, and concurrency and
connection limits; a per-client rate limit is left to a proxy in front of it.
