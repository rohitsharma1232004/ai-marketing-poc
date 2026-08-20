# Single Senior Approval Workflow

## Active POC flow

```text
Client details
  -> Groq / n8n generation
  -> validated Calendar Version 1 saved in SQLite
  -> Pending Senior Review
     -> Approve -> Excel download unlocked
     -> Request Changes -> Excel remains locked
          -> user selects the affected post
          -> AI regenerates only that post
          -> Calendar Version 2 is saved
          -> Pending Senior Review again
```

Only the Senior is an approval gate in the active POC. Client approval and WhatsApp review delivery are not part of this flow.

## Approval rules

- Generation saves an immutable, versioned calendar with a content hash.
- Senior decision is stored against the exact calendar version and content hash.
- Senior reviewer name and email are recorded locally for the audit trail.
- Request Changes requires feedback.
- After a rejection, the user can select one post to regenerate from the Senior feedback.
- Selected-post regeneration preserves **Date, Platform, Pillar, and Format** and rewrites only **Content Idea, SEO Keyword Focus, and CTA**.
- A successful selected-post regeneration creates a new immutable calendar version and returns the campaign to `Pending Senior Review`.
- The previous rejection remains part of the audit history and does not apply to the new version.
- Senior approval is terminal for the active POC and moves the campaign to `fully_approved`.
- Excel export is allowed only when the latest calendar has a matching Senior `approved` decision.
- A stale approval for an older calendar version never unlocks a newer version.

## Compatibility

The database schema retains previous two-stage approval and WhatsApp-review structures so historical campaigns remain readable. Client approval is not requested by the active application.

The previous two-stage/WhatsApp code and documentation remain under `legacy_approval_whatsapp/` for possible future reuse.

## Next increment

Add a secure shareable Senior Review URL for deployed environments. The Senior page should expose only the calendar, Approve, Request Changes, and feedback controls; it should not expose generation/admin settings.
