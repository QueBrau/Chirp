# Mobile contract verification (c360)

`npm run verify:contract` compares actual mobile HTTP calls with backend source,
then runs deliberately broken examples against the same checker. It needs the
locked mobile dependencies and Python 3's standard library; no API server, backend
package installation, database, or credentials are involved.

The previous regular-expression gate omitted `requestText`, including CSV exports.
Comments also looked like unresolved calls without failing the gate. The new
TypeScript AST extractor follows named imports and aliases of `request`,
`requestText`, and `requestWithHeaders` from `./client`; unresolved call paths,
options or methods fail. Python AST supplies router/query and declared schema
metadata, including response models declared in router modules, without importing
the backend application.

The gate checks:

- HTTP method and path, including CSV calls; template parameters occupy complete
  path segments. Unknown extraction fails rather than silently dropping a call.
- Declared query keys using TypeScript's type checker, including optional keys and
  inherited interfaces. Unbounded keys cannot establish a contract.
- Required top-level response fields when the server and client both declare a
  structured model, including arrays and inherited fields.
- Required top-level fields of the current `message` and `poll` WebSocket event
  interfaces against their literal server publishers.

The first run found a real response drift: mobile `FeedPostOut` inherited required
`deleted_at` from the create-response DTO, but backend list responses omit it. The
list DTO now explicitly omits that field. The create and comment DTOs retain it.

Twelve isolated child-process mutations must fail for the expected reason: wrong CSV
route, wrong aliased CSV route, unresolved CSV path, unresolved method, wrong query
key, a query hidden in an options spread, wrong client response field, missing server response field, wrong client event
field, missing server event field, shorthand method and router-local response
field. Mutations operate on the real source in
memory or its extracted inventory; no repository file is rewritten. A disappeared
fixture anchor is a failing self-check, not an accepted result.

The auth integration introduced opaque `fetchMe` and `getMediaUploadUrl` options.
These helpers now accept only operation/deadline/cancellation fields and forward
them explicitly with the fixed method/body, so transport controls remain available
without hiding method/query changes from the gate. Independent review reproduced
the shorthand-method and router-local-response omissions before they were fixed.

This does not validate JSON at runtime, field values/nullability, nested payloads,
request bodies, response headers, every possible publisher, authorization or
business semantics. Optional fields are not required by the presence checks. New
wrapper modules or a different publishing style require deliberate coverage. The
TypeScript compile and database-backed API tests remain separate required gates.

Async correctness also needs executed behavior tests. The session/socket and event
pagination verifiers exercise their actual implementation with controlled
dependencies; read their case names and corresponding cards for the covered
scenarios. c358's broader page ownership work remains separate. Registration in
`verify:ci-coverage` proves invocation, not the quality of an assertion, native
rendering, Firebase/Stripe SDK behavior or deployed WebSocket delivery.

The c360 CI repair also replaces the photo-finalization test's short sleep overlap
window with a bounded two-party barrier. Both request finalizers must be inside the
worker region before either can finish. All three file tests passed; replacing the
actual async finalization helper with inline synchronous work failed specifically
at the barrier. Slow scheduling can delay rendezvous without changing the property
being tested. The finite timeout still fails a true deadlock or missing participant.
