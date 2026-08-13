# Use daily backups with point-in-time recovery

## Context

A warm standby does not protect against operator error, corruption, or delayed
discovery. The production profile needs a tested recovery history.

## Accepted choice

Archive PostgreSQL recovery logs continuously. Create an encrypted full backup
each day. Support point-in-time recovery for 35 days. Run an automated restore
test at least monthly and record its recovery results.

## Alternatives

- Use full backups without recovery logs.
- Keep a shorter recovery window.
- Test restore only during a disaster-recovery exercise.

## Good effects

- Operators can select a recovery point between full backups.
- The 35-day window covers delayed discovery across a monthly cycle.
- Monthly tests find unusable backups before an incident.

## Bad effects

- Recovery-log retention and daily backups use storage and operating time.
- Restore tests need isolated infrastructure and protected key access.

## Migration effect

Production deployment and operations must configure backup storage, key
custody, recovery-log continuity checks, retention, and monthly test jobs.

## Security effect

Backups must be encrypted, access-controlled, integrity-checked, and separate
from the writable database host. Test output must not expose sensitive data.

## Review conditions

Review the schedule or window when measured data growth, recovery exercises,
or legal retention requirements require a different accepted value.
