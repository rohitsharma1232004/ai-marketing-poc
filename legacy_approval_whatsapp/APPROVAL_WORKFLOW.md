# Senior-to-Client Approval Workflow

This document separates the working local proof of concept (POC) from the next
production design. The central rule is that the application database is the
approval authority. n8n sends notifications and coordinates work, but it never
creates an approval or decides that content is safe to publish.

## Current development implementation

The implementation uses Streamlit, a narrow FastAPI review portal, SQLite, and
a stateless n8n WhatsApp notification workflow:

1. Successful generation saves an immutable calendar version and moves the
   campaign to `pending_senior_review`.
2. A reviewer can enter the full Campaign ID in the local dashboard to load the
   latest calendar, its status, and its approval history. A Campaign ID is a
   reference, not a password or authorization credential.
3. A campaign can use the local name/email fallback or consented WhatsApp
   reviewers. WhatsApp mode creates a signed, expiring senior link and disables
   local decision controls for that campaign.
4. The client can decide only after a senior approval exists for the same
   calendar version and content hash. Client approval moves the campaign to
   `fully_approved`; client rejection moves it to `revision_required`.
5. The final approved Excel is created and offered for download only after
   `fully_approved`. A senior approval alone does not create the final export.
6. `Clear View` clears the browser view only. It does not remove campaign,
   calendar, or approval history from SQLite.

Local-form identity remains self-reported. A WhatsApp link proves possession of
that bearer link, but it can be forwarded and therefore is not strong identity
proof without OTP/login. The development database retains legacy statuses:
`pending_review` can enter the senior stage, while legacy `approved` means
only the old single-step flow and is not treated as `fully_approved`.

### Status lifecycle

| Status | Meaning | Allowed next result |
| --- | --- | --- |
| `generating` | A new version is being created | `pending_senior_review`, a known failure, or an unknown outcome |
| `generation_failed` | Generation definitely failed | Explicit retry to `generating` |
| `generation_unknown` | A timeout left the result uncertain | Reconcile first, then explicit retry if needed |
| `pending_senior_review` | Latest version awaits the senior | Approve to `pending_client_review`; reject to `revision_required` |
| `pending_client_review` | The same senior-approved version awaits the client | Approve to `fully_approved`; reject to `revision_required` |
| `revision_required` | Human feedback requires a replacement version | Regenerate through `generating` |
| `fully_approved` | Both roles approved the latest version and hash | Terminal for the current local POC |

```text
generated
   -> pending_senior_review
      -> rejected -> revision_required -> new version -> pending_senior_review
      -> approved -> pending_client_review
         -> rejected -> revision_required -> new version -> pending_senior_review
         -> approved -> fully_approved -> final export -> scheduling may be requested
```

### Version and hash binding

Every generated calendar is a new immutable version with its own UUID and
SHA-256 content hash. The hash covers the calendar headers and rows plus the
allowlisted client and generation metadata stored with that version.

Before either decision, the store recomputes the hash, requires the latest
version, checks the campaign's current stage, and records the decision and
status change atomically. Senior and client approval rows are append-only and
bound to the exact `campaign_id`, `calendar_version_id`, role, and hash. If the
calendar changes, the application creates a new version; the old decisions stay
in the audit history but do not approve the replacement. Both reviewers must
approve the replacement again.

## Production boundary

```text
Reviewer browser --public HTTPS--> FastAPI review portal --transaction--> PostgreSQL
                                      |                         |
                                      | outbox event            | canonical state
                                      v                         |
                               private n8n workflows <----------+
                                      |
                                      +--> WhatsApp Business Cloud
                                      +--> reminders / escalation
                                      +--> export and scheduling orchestration
```

- FastAPI owns authentication, authorization, token validation, status rules,
  version/hash checks, decision writes, and the final scheduling gate.
- PostgreSQL owns campaigns, immutable versions, review requests, approvals,
  notification delivery records, idempotency records, and the audit log.
- n8n owns notification delivery, waiting, reminders, escalation, retries, and
  workflow visibility. It reads canonical status from FastAPI before branching.
- Only the review portal and narrowly scoped API routes are public over HTTPS.
  Keep the n8n editor, PostgreSQL, internal callbacks, and admin routes private.
  For self-hosted n8n behind a proxy, configure the public webhook base URL as
  described in n8n's [reverse-proxy webhook guidance](https://docs.n8n.io/hosting/configuration/configuration-examples/webhook-url/).

### WhatsApp notification flow

1. The application saves the exact calendar version and creates a hash-only
   senior review request plus an identifier-only outbox job.
2. The dispatcher reconstructs the deterministic signed token in memory and
   calls the authenticated, published n8n production webhook.
3. The six-node notification workflow validates a locked contract and uses the
   native WhatsApp Business Cloud node to send the approved Utility template.
4. The reviewer opens `/r/<token>`. GET only renders an interstitial. A
   deliberate POST exchanges the one-use link for a short-lived HttpOnly
   session; a second POST submits Approve or Request changes with CSRF binding.
5. SQLite/FastAPI verifies role, current stage, expiry, latest version, and
   content hash before committing. Senior approval atomically creates the
   client request and outbox job. Client approval moves to `fully_approved`.
6. Notification failure never rolls back a human decision. The durable outbox
   remains retryable, while Streamlit reads canonical campaign status.

n8n never waits for, resumes, or records the approval decision. There is no
`$execution.resumeUrl` in the public flow. The setup is documented in
`n8n_workflows/WHATSAPP_APPROVAL_SETUP.md`.

### Conceptual contracts

Exact paths may change; version the payloads from the first production release.

| Contract | Required conceptual fields |
| --- | --- |
| App -> n8n `marketing.whatsapp-review-notification.v1` | `event_id`, request/campaign/version IDs, content hash, role, recipient name/E.164 phone, due time, signed URL suffix |
| n8n -> app response | Matching contract/event/request IDs, accepted status, provider, and Meta message ID |
| Browser -> FastAPI decision | Hash-backed review session cookie, CSRF token, matching request ID, decision, and feedback; identity comes from the stored recipient |
| n8n -> FastAPI scheduling authorization | `campaign_id`, `calendar_version_id`, `content_hash`, platform/account, requested time, `Idempotency-Key` |

Internal calls need a scoped service credential or signed request with timestamp
and replay protection. Contracts must not include API keys, raw prompts,
uploaded document text, or unnecessary client data.

### Signed review links

- A link is short-lived, single-purpose, and bound to the review request,
  recipient, role, Campaign ID, calendar version, content hash, and expiry.
- Store only a hash of the opaque token. Exchange a valid link for a scoped
  session; a `GET` must never approve or reject. The decision is an authenticated
  CSRF-protected `POST`.
- Use organization login/SSO for seniors. Clients should use an account or a
  one-time signed link with an additional verification step where risk warrants
  it. Expiry leaves the campaign pending and creates a replacement link; it
  never implies approval or rejection.
- The Campaign ID may be displayed and searched by authorized users, but knowing
  it alone grants no access.

### Rejections, retries, and idempotency

- A rejection always records mandatory feedback and moves the campaign to
  `revision_required`. Regeneration creates a new version/hash and restarts at
  senior review; client review cannot bypass the new senior decision.
- Commit status, approval, and an outbox event together. Deliver events at least
  once and deduplicate by `event_id`/`review_request_id` rather than assuming
  exactly-once delivery.
- A repeated decision with the same idempotency key returns the original result.
  A different payload for the same key, a duplicate role decision, stale
  version, wrong hash, or invalid stage returns a conflict without changing
  state.
- Give WhatsApp sends a unique delivery key. Retry network errors, HTTP 429,
  and 5xx responses with bounded
  exponential backoff and jitter; do not retry validation, authentication, or
  business-rule failures automatically.
- A Wait timeout sends a reminder or escalation and expires/reissues the link;
  it never creates a human decision. Route terminal failures to a separate error
  workflow and preserve the Campaign ID and correlation ID. n8n also supports
  controlled retries from its [Executions view](https://docs.n8n.io/workflows/executions/all-executions/).

## Non-bypassable publishing rule

Every scheduling attempt must ask FastAPI for a fresh authorization using the
exact Campaign ID, latest version ID, and content hash. In one database
transaction, FastAPI must verify `fully_approved` and two approved, hash-matched
decision rows for that latest version. Only then may it issue a short-lived
scheduling authorization/job ID. n8n and platform connectors must refuse to
schedule or publish without it; an earlier email, Excel file, cached workflow
value, or Campaign ID alone is never sufficient.
