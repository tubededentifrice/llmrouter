# Proxy protected exports and version operations

## Context

Captured exports contain sensitive content. Operations also need a stable
headless interface with the same authority as the hosted application.

## Accepted choice

Redeem captured exports through a same-origin Router endpoint. Require the
current administrator session, content-read grant, recent authentication, and
a short-lived one-use token. Do not return a direct or presigned object-store
URL. Provide versioned headless operations for all hosted operational actions.
Keep configuration revisions by both minimum count and age.

## Alternatives

- Return a presigned object-store link.
- Limit operations to the hosted application or local CLI.
- Retain configuration revisions by count or age, but not both.

## Good effects

- Router authorization remains active at export redemption.
- Hosted and headless operations use one policy and audit model.
- Count and age retention protect both recent activity and long quiet periods.

## Bad effects

- The Router must proxy export bytes.
- The operations API and audit model cover more routes.
- Revision deletion needs evaluation of two limits.

## Migration effect

The formal API must replace direct result URLs, add redemption, expose
operational routes, and include both revision-retention values.

## Security effect

Exports use no-store and no-referrer controls. Sensitive operations require
recent authentication, narrow grants, and audit events.

## Review conditions

Review export delivery if measured proxy load cannot meet requirements without
weakening authorization at redemption.
