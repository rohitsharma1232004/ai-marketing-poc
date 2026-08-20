# Single Senior Approval Workflow

## Active POC flow

```text
Client details
  -> Groq / n8n generation
  -> validated calendar saved in SQLite
  -> Pending Senior Review
     -> Approve -> Excel download unlocked
     -> Request Changes -> Excel remains locked
```

Only the Senior reviews in the active Streamlit app. Client approval and WhatsApp review delivery are not part of this POC.

## Approval rules

- Generation saves a versioned calendar with a content hash.
- Senior decision is stored against the exact calendar version and content hash.
- Senior reviewer name and email are recorded locally for the audit trail.
- Request Changes requires feedback.
- Excel export is allowed only when the latest calendar has a matching Senior `approved` decision.
- Reopening by Campaign ID re-checks the stored Senior approval before showing the Excel download.
- A stale approval for an older calendar version does not unlock a newer version.

## Compatibility

The database schema retains the previous two-stage approval and WhatsApp-review structures so historical campaigns remain readable. In the active application, a stored Senior approval is the terminal approval required for Excel export; Client approval is not requested.

The previous two-stage/WhatsApp code and documentation are preserved under `legacy_approval_whatsapp/` for possible future reuse.
