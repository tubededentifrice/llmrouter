# Cross-service conformance suite

## Purpose

LLM Router, Ontology, and Xbot MUST pass this suite before the first release
and before a pinned contract or supported major version changes. Tests use
isolated services and workspaces. They MUST NOT use production credentials or
data.

Each test records the contract artifacts and SHA-256 values, runtime contract
manifest, service and workspace identities, operation identities, final
states, audit identities, and safe failure details. A test MUST fail when a
required operation or capability is absent even when the major version is
accepted.

## Contract and identity tests

1. Build Xbot from the exact pinned Ontology and Router artifacts. Change one
   pinned byte and verify that the build provenance gate fails.
2. Start Xbot against each service with the supported major and all required
   capabilities. Verify success even when a compatible optional field exists.
3. Remove one required capability or change a major version. Verify that Xbot
   reports the exact incompatible dependency and does not start normal work.
4. Exchange each Xbot service secret only for its intended service, audience,
   workspace range, and operations. Try each token on the other service, a
   different audience, a different workspace, and a global administration
   route. Every attempt MUST fail before record access.
5. Verify that one Pocket ID account can authenticate to the separate
   Ontology and Router administrator clients. Verify that neither application
   grants authority from identity alone. Verify that an Xbot passkey session
   cannot authenticate to Pocket ID or either global application.

## Workspace lifecycle tests

1. Create one Xbot workspace with one logical provisioning identity. Lose each
   response in turn after Xbot commit, Ontology commit, and Router commit.
   Restart Xbot after each loss. Reconciliation MUST produce exactly one Xbot,
   Ontology, and Router workspace mapping.
2. Reuse an idempotency key or caller reference with different content. The
   owning service MUST reject it without a second workspace.
3. Disable, restore, and delete or retire from every partial state. Ontology
   deletion and Router retirement MUST remain distinct. A retired Router
   identity and deleted Ontology workspace identity MUST never be reused.
4. During reconciliation, attempt record access with the wrong service or
   workspace. The response MUST not reveal whether the hidden record exists.

## Request and recovery tests

1. Lose the response before admission, during durable admission, after one
   provider attempt, after visible output, and after a committed business-tool
   effect. The official client MUST keep the UUIDv7 and request fingerprint.
   The Router MUST create no second logical request or repeated effect.
2. Submit one UUIDv7 with changed input, assignment, tool list, budget, output
   control, attachment digest, service, and workspace. Each mismatch MUST
   produce `request_identity_conflict` without prior content disclosure.
3. Fail each provider error scope. Verify candidate skipping, fallback, health
   scope, attempt accounting, and the public terminal error.
4. Disconnect each stream before output, after one `output.delta`, after
   `tool.started`, and before `request.terminal`. Replay MUST keep sequence and
   content. Expired replay MUST direct the client to status. Fallback MUST not
   occur after a commit boundary.
5. Cancel in `admitted`, `running`, `waiting_for_tool`, and each terminal
   state. Verify idempotency, no new effects, late accounting, and terminal
   `uncertain` after 10 minutes when stop evidence never arrives.
6. Upload an attachment with a wrong digest, wrong length, unsupported media
   type, excessive size, excessive count, and wrong service or workspace.
   Every operation MUST fail before request admission. A complete attachment
   MUST be immutable and readable only through its authorized Router endpoint.
7. Run eight timed-out candidates and verify that a ninth attempt cannot start.
   Verify the 120-second attempt limit and 15-minute logical limit. A late
   attempt MUST be shortened or skipped.
8. Submit compatible exact routing with one missing extension field, an exact
   route encoded in `model`, a wrong grant, and a valid pair. Only the valid
   explicit pair can use the approved exact route.

## Embedding tests

1. Pin the `embedding_requests_v1` capability and major version 1 of the
   `embedding_protocol` artifact. Remove the capability and change the major
   version in turn. Ontology MUST stop embedding work and name the incompatible
   pin.
2. Submit 1 and 32 inputs, a 32768-byte input, and a 262144-byte batch. Exceed
   each batch, item, total-input, dimension, 30-second attempt, four-attempt,
   120-second logical, and 8388608-byte response limit in turn. The Router MUST
   reject or fail the request without a partial vector result.
3. Configure two fallback candidates with the same opaque model-space identity
   and exact dimension. Fail the first candidate. The complete batch MUST use
   the second candidate and return one ordered atomic result. Accounting MUST
   include both attempts.
4. Change one fallback candidate to a different model-space identity and then
   to a different dimension. Assignment publication MUST fail. Submit a
   request with the wrong model space and dimension. Each request MUST return
   `embedding_space_mismatch` before provider work.
5. Lose the create response before and after admission. Repeat the same UUIDv7
   and body. The Router MUST create one logical batch. Change the assignment,
   input policy, model space, dimension, input identity, input order, digest,
   text, and optional `max_cost` in turn. Each change MUST return
   `request_identity_conflict` without input or vector disclosure.
6. Make one batch item fail after another item has a provider result. The
   terminal status MUST contain no vector. Logs, metrics, accounting, audit,
   and safe errors MUST contain no input text, input digest, or vector.
7. Remove and then exhaust the exact workspace hard budget with no request
   `max_cost`. Admission MUST fail before provider work in each case. Add a
   lower `max_cost` and verify that it also limits all fallback attempts. A
   failed billable attempt MUST remain in batch and workspace accounting.
8. Delete the Ontology source after Router admission. The Ontology vector MUST
   follow Ontology deletion rules. Router capture MUST stay until its recorded
   admission-time expiry, and no result MUST claim that source deletion removed
   the Router copy.
9. Submit one input with a digest that does not match its exact UTF-8 text
   bytes. The Router MUST reject the batch before admission. Use the same text
   with a changed input-policy identity. The repeat MUST return
   `request_identity_conflict`.
10. Use the original token to read status through another service and another
    workspace path. Then use tokens for those other scopes. Each wrong-scope
    use MUST fail before record access and MUST NOT disclose the first binding.
    The same UUIDv7 in another authorized scope is an independent scoped
    identity and MUST NOT return an identity conflict caused by the first
    scope.
11. Return a non-finite value and then a wrong vector dimension, count, input
    identity, and order from the provider adapter. Each batch MUST fail as an
    invalid provider response and MUST return no vector.

## Authorization and administration tests

1. Open each Ontology and Router frame from an allowed Xbot origin. Try a
   wildcard origin, wrong source window, replayed bootstrap token, changed
   user, changed workspace, lost membership, and expired session. The frame
   MUST lose authority without showing prior-scope data.
2. Create a read-only frame. Try to add a write permission in browser code.
   It MUST fail. Create a sensitive frame with Xbot passkey authentication
   older than five minutes. It MUST fail.
3. Set the Xbot-owned Router workspace ceiling. Try to raise or remove it from
   global Router administration and the hosted service frame. Both attempts
   MUST fail. A permitted actor can set a lower assignment limit.
4. Disable an Xbot, Ontology, or Router workspace during active work. Verify
   that new admission stops and retained content remains hidden from the
   service-scoped frame.
5. Create, narrow, expire, and revoke local grants. Verify that a delegating
   actor cannot expand its own scope. A service administrator MUST be able to
   select an eligible credential reference and MUST NOT change secret material.
6. Create a captured-content export. Verify that a direct object-store URL is
   never returned. Redemption without the current session, content-read grant,
   recent authentication, or valid one-use token MUST fail and be audited.

## Outage and recovery tests

1. Make Ontology unavailable. Xbot MUST show clearly stale cached data as
   read-only and MUST permit no domain or external mutation.
2. Make Router unavailable while Ontology stays healthy. Xbot MUST allow
   manual Ontology edits. It can run a deterministic destination write only
   when the exact approved package exists and all current Xbot, Ontology, and
   destination checks pass. Model and shared-tool work MUST remain unavailable.
3. Create an Ontology feed gap, webhook duplicate, and delayed delete cleanup.
   Xbot MUST resynchronize from the supplied boundary and MUST not duplicate a
   domain effect.
4. Fail the Router primary with active requests, a cancel request, a stream
   commit, an unconfirmed tool intent, and spooled accounting. Promotion MUST
   meet the RTO and RPO rules and MUST repeat no external effect.
5. Fill the Router spool through warning, shedding, stop, and emergency
   thresholds. Verify visible capture state, admission order, reserved
   capacity, and no missing canonical accounting or audit event.
6. Drain a node for 15 minutes and at the 30-minute maximum. Verify that new
   admission stops, fenced transfer prevents duplicate ownership, and a forced
   stop does not report a clean drain.
7. Restore a daily full backup and replay continuous recovery logs to selected
   points in the 35-day window. Verify integrity, critical record counts, and
   the recorded monthly restore-test result.

## Account deletion and retention tests

1. Delete one Xbot account. Within 24 hours, its private agent conversations,
   saved Ontology views, notification preferences, private notification links,
   and other member-specific records MUST be absent. Shared work keeps only a
   value-free former-actor marker.
2. Verify that Xbot source deletion does not claim to delete retained Router
   capture. Router content expires only under its admission-time retention
   rule.
3. Run each Ontology and Router export, retention expiry, and workspace cleanup
   through worker restart and retry. Each result MUST be idempotent and each
   protected read or change MUST have one audit trail.

## Release result

The release report MUST list every case as passed, failed, or not applicable
with an accepted reason. A contract, isolation, repeated-effect, accounting,
auth, deletion, RTO, or RPO failure blocks release. A dependency security scan
with an unresolved critical or high finding also blocks production release.
