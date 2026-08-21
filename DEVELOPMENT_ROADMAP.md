# Current POC scope override — August 20, 2026

The active demo is now intentionally limited to:

`Client details -> generation -> validated content calendar -> Senior approval -> Excel download`

Only one human gate is active: **Senior approval**. Client approval and WhatsApp delivery are archived under `legacy_approval_whatsapp/`. The active Streamlit app now supports a secure Senior-only share-link mode so the same deployment can serve both the internal dashboard and the restricted review page.

The database keeps older two-stage fields/statuses for compatibility, but the active Excel gate checks only for a hash-matched Senior approval on the latest calendar version.

# AI Marketing Operations System — Development Roadmap

The content calendar is one component of the future system. Development follows
vertical milestones so every stage can be tested before it receives publishing
permissions.

## Milestone 1 — n8n generation gateway

Status: **complete and live-verified on August 19, 2026**

- Preserve the existing Streamlit form and deterministic calendar validators.
- Add explicit `groq` and `n8n` generation providers.
- Add a versioned request/response contract and correlation ID.
- Add an importable n8n workflow with webhook and Groq credentials kept in n8n.
- Add mocked provider tests and a Streamlit-to-webhook integration test.

Acceptance completed:

- Imported and published the six-node workflow in the user's self-hosted n8n.
- Configured separate webhook and Groq Header Auth credentials.
- Ran a real Streamlit request through the production webhook and Groq.
- Confirmed the resulting n8n execution completed successfully (green).

## Milestone 2 — persistent campaigns and progress

Status: **development POC complete; production migration in progress**

Milestone 2A completed locally:

- Added SQLite clients, campaigns, immutable calendar versions and workflow events.
- Correlated every n8n request with persisted campaign and request UUIDs.
- Added atomic transition from generation to pending_senior_review.
- Preserved immutable version/hash-bound approval records while the active POC now
  uses one terminal Senior approval before Excel export.
- Added full Campaign ID lookup and visible recent history; clearing the current
  view never deletes records.
- Kept uploaded document bytes/text and all credentials out of the database.
- Added schema v3 hash-only signed review requests/sessions, consented WhatsApp
  recipients, and a durable notification outbox.
- Added a narrow FastAPI portal where GET cannot approve, session exchange is
  one-use, decision POSTs are CSRF protected, and exact calendar content is escaped.
- Added an importable official WhatsApp Business Cloud n8n template workflow.

Remaining for Milestone 2:

- Move the canonical schema to PostgreSQL and put admin APIs behind authentication.
- Return asynchronous job IDs and display n8n stage-by-stage progress callbacks.
- Add authenticated, tenant-scoped campaign and approval APIs.
- Store documents/assets outside n8n; pass identifiers and bounded excerpts.

## Milestone 3 — evidence-backed research and strategy

Status: planned

- Client knowledge and approved-claims store.
- Separate business, audience, competitor and trend research sub-workflows.
- Save URL, title, excerpt, retrieval date and confidence for every finding.
- Generate a structured business analysis and campaign strategy before content.
- Keep research/tool use separate from strict JSON synthesis.

## Milestone 4 — complete content package

Status: planned

- Content ideas and calendar.
- Captions, hashtags, carousel slides and reel scripts.
- Design briefs and platform variants.
- Automatic brand, claim, duplication and platform QA.
- Freeze and hash each content-package version.

## Milestone 5 — Senior approval and revision

Status: **secure-link development POC complete; production hardening planned**

- Active Streamlit uses one terminal Senior approval and records names, emails,
  decisions, feedback, timestamps, version IDs, and content hashes.
- Local final Excel creation is allowed only after the latest version has matching
  Senior approval.
- Structured Senior change requests support Specific Post or Whole Calendar scope and field-level selection for Content Idea, SEO Keyword Focus, and CTA.
- The marketing user can add optional instructions, while the Senior's original required-changes description remains preserved.
- Field-level regeneration creates a new version and requires fresh Senior review.
- Secure opaque Senior Review URLs are implemented in the active Streamlit app; raw capability tokens are never stored.
- Add OTP/login/SSO before production so a forwarded bearer link is not sufficient
  identity proof.
- Field-level revision is implemented: a Senior can request, for example, SEO-only changes on Post 5 or CTA-only changes across the whole calendar. Non-requested fields are preserved exactly before the new immutable version is saved.
- Set `APP_PUBLIC_BASE_URL` to the deployed HTTPS URL and move durable state off ephemeral local disks before production use.
- n8n sends notifications and reacts to decisions; PostgreSQL remains the
  approval authority.

## Milestone 6 — creative production

Status: planned

- Start with complete design briefs and Canva handoff.
- Add approved Canva templates and Autofill where account eligibility allows.
- Keep an interchangeable design-provider interface for other image/video APIs.

## Milestone 7 — publishing and analytics

Status: planned

- Start with one publishing integration and one platform pilot.
- Recheck both approvals and content hash immediately before publishing.
- Add idempotency keys, OAuth refresh handling, retries and a kill switch.
- Collect normalized metrics after 24 hours, 7 days and 30 days.
- Generate recommendations; only verified learnings update future campaigns.

## Architecture rule

For the small internal POC, one Streamlit deployment owns the dashboard and the token-gated Senior review view, which avoids a second service and shared-filesystem problems. n8n owns external automation connections. The persistence layer remains isolated behind `CampaignStore`; before production, move durable state/approvals to a managed persistent database. Groq creates analysis/content only. The language model never receives unrestricted publishing credentials and never decides whether required approval exists.
