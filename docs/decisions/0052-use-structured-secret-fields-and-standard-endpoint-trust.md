# Use structured secret fields and standard endpoint trust

## Context

Broad secret-pattern scanning can reject valid service content and cannot prove
that arbitrary content has no secret. Endpoint trust must also remain clear and
compatible with normal certificate operations.

## Accepted choice

Reject secrets in structured control fields and redact known authenticated
control values before they leave the receiving process. Do not classify
arbitrary content only through broad secret-like patterns in the first release.
For non-loopback HTTPS, use normal certificate-authority validation and exact
hostname checks. Permit optional SPKI pins as an additional check. Permit
plaintext HTTP only on explicit loopback endpoints.

## Alternatives

- Scan and reject all content that resembles a secret.
- Trust a certificate pin without normal certificate validation.
- Permit plaintext HTTP on private networks.

## Good effects

- Valid prompts and tool content avoid unpredictable pattern rejection.
- Known control values still receive deterministic protection.
- Standard certificate renewal works without disabling hostname validation.

## Bad effects

- The Router cannot promise detection of a secret placed in arbitrary content.
- Optional pins need an overlap process during certificate-key rotation.

## Migration effect

Contracts must mark secret and write-only fields. Clients must implement the
accepted endpoint trust profile and loopback restriction.

## Security effect

This choice concentrates controls on authenticated and structured values. It
keeps normal TLS validation active and uses pins only for extra protection.

## Review conditions

Review content scanning only if a defined data profile requires it and has
measured false-positive and false-negative bounds. Review endpoint trust when
deployment certificate policy changes.
