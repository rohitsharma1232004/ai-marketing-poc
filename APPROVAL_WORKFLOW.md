# Single Senior Approval Workflow

## Active POC flow

```text
Client details
  -> Groq / n8n generation
  -> validated Calendar Version saved
  -> Pending Senior Review
  -> user creates secure Senior Review Link
  -> Senior opens link in a separate browser/device
       -> sees only client/campaign/version + calendar + decision controls
       -> Approve -> Excel download unlocked
       -> Request Changes -> Excel remains locked
            -> Senior chooses Specific Post or Whole Calendar
            -> Senior selects only the field(s) that need changes
               (Content Idea / SEO Keyword Focus / CTA)
            -> Senior enters a mandatory Required Changes Description
            -> marketing user may add optional extra instructions
            -> AI regenerates only the requested field(s) and scope
            -> new immutable Calendar Version is saved
            -> new Senior Review Link is required
```

Only the Senior is an approval gate in the active POC. Client approval and WhatsApp delivery are disabled.

## Share-link security rules

- The review URL contains a cryptographically random capability token, not a Campaign ID.
- Only a domain-separated SHA-256 hash of the token is stored in SQLite; the raw token is never persisted.
- Every link is bound to the exact latest calendar version and content hash.
- Links expire (72 hours by default), are revocable, and can save only one decision.
- Creating a replacement link revokes the previous pending link for that version.
- Opening a link does **not** approve anything.
- The Senior must explicitly press **Approve Calendar** or **Request Changes**.
- Request Changes requires a structured scope, at least one editable field, and a Required Changes Description.
- The link view bypasses the main Streamlit dashboard, so generation controls, history, API configuration, and Excel download are not rendered to the Senior.
- Senior name/email are self-reported in this internal POC. Add login/SSO later if stronger identity assurance is required.

## Version and export rules

- Generation saves an immutable, versioned calendar with a content hash.
- Senior decision is stored against the exact calendar version and content hash.
- Senior change requests are stored separately from the immutable approval record with scope (`Specific Post` / `Whole Calendar`), target post (when applicable), selected fields, and the original required-changes description.
- Field-level regeneration always preserves **Date, Platform, Pillar, and Format**. It can change only the Senior-selected subset of **Content Idea, SEO Keyword Focus, and CTA**.
- Marketing-team additional instructions are optional add-ons and cannot replace the Senior's original request.
- A successful field-level regeneration creates a new immutable version and returns the campaign to `Pending Senior Review`.
- An older approval never unlocks a newer version.
- Senior approval is terminal for the active POC and moves the campaign to `fully_approved`.
- Excel export is allowed only when the latest version has a matching Senior `approved` decision.

## Local testing

`APP_PUBLIC_BASE_URL = "http://localhost:8501"` creates a link that can be opened on the same computer.

## Deployment

Deploy the Streamlit application once and set:

```toml
APP_PUBLIC_BASE_URL = "https://your-real-app-domain.example"
SENIOR_REVIEW_LINK_TTL_HOURS = 72
```

The link logic does not depend on localhost, so deployment changes only the public base URL. For durable multi-user deployment, move the canonical database from local SQLite to a managed persistent database before relying on the system for production records.

## Compatibility

Historical two-stage/WhatsApp tables and archived code remain under `legacy_approval_whatsapp/`. They are not used by the active Senior-only share-link flow.
