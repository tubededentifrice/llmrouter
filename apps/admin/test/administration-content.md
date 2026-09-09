# Administration content checks

The accepted [Administration content rule](../../../docs/specs/04-authentication-administration-and-shared-ui.md#administration-content)
is the requirement source. This document records the test scope. It does not
add product requirements.

The exact reviewed strings, route, surface, expected presence, and keep reason
are in [the inventory](fixtures/administration-content.json). The
[browser check](administration-content.browser.mjs) compares rendered content
with that inventory at desktop and phone widths. Keep the inventory and its
fixtures current when route content changes.

| Route                        | Source modules                                             | Checked surfaces                                                                                                               |
| ---------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `/overview`                  | `App.tsx`                                                  | Resource totals, health, cooldown total, refresh and read states                                                               |
| `/services`                  | `App.tsx`, `ServiceManagement.tsx`                         | Tree, compact inspector, create form, discard and read/write states                                                            |
| `/services/{serviceApiName}` | `ServiceDetails.tsx`, `ServiceAccess.tsx`                  | Heading, facts, form, workspaces, keys, secret, deletion and read/write states                                                 |
| `/configuration`             | `App.tsx`, `ConfigurationGraph.tsx`, `PlaygroundModal.tsx` | Three columns, provider/model/route/assignment inspectors, advanced forms, import, deletion, playground and conditional states |
| `/logs`                      | `LogsPage.tsx`, `LogsDetail.tsx`, `logsState.ts`           | Filters, retention, records, selected details, retained media and read states                                                  |
| `/statistics`                | `StatisticsPage.tsx`, `statisticsState.ts`                 | UTC date effect, basic and advanced filters, group limits, corrective errors and all query states                              |
| `/operations`                | `App.tsx`, `administrationSafety.ts`                       | Health messages, retention, cooldowns, activity and read states                                                                |

Headings, labels, actions, record values, corrective errors, state messages,
accessible names, and live regions are outside the static-helper removal
rule. The checks preserve these items. Each retained helper has one reason
from the accepted rule. The statistics UTC note explains inclusive calendar dates;
the Operations retention note explains which data can expire; the credential
and playground notes explain authority and secret handling. The service and
workspace deletion notices explain the records that deletion removes.

Health messages are optional API record content. The current backend supplies
component names and status values without messages. The browser fixtures also
supply corrective and safety messages to check their presentation. Both health
surfaces omit the message element when no message is supplied. No placeholder
or activity-history disclaimer replaces the removed text.

Use the installed shared OpenDLE UI package and its browser tools. The check
uses the real `App` with controlled client fixtures at
`http://127.0.0.1:5174`. It does not require a live backend or a test session.
Synthetic records and one-time secrets cannot authenticate. All calls stay in
the fixture; no provider call or live write occurs.

```sh
node apps/admin/test/administration-content.browser.mjs
```

The test creates screenshots and Axe evidence outside Git. Review the normal
route images and the conditional images after a relevant content change.
Do not replace dependencies while a browser check is running.

The retention reduction uses a native browser confirmation. The test records
its exact message and dismisses it. Page screenshots cannot capture native
browser controls. The corresponding screenshot shows the underlying page.

The statistics fixtures cover the accepted UTC date-only filters, the advanced
filter disclosure, the group limit, the five date errors, and loading, empty,
failed and ready query states. The focused [statistics browser check](statistics.browser.mjs)
also verifies exact HTTP queries in four time zones, later-query state, keyboard
access, focus, desktop and phone layouts, and 200% text. Native date checks
inspect the browser shadow fields, complete year, and calendar button. Radio
checks use Tab, Shift+Tab, and arrows, and measure full words, rows, and focus
outlines against the phone navigation. Layout checks compare common page
gutters and the shared result-card layout at 320, 390, and 1440 pixels. All
statistics states use the real application with synthetic loopback responses.
