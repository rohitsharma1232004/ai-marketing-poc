# Creative Automation Architecture

Last reviewed against official provider documentation: 2026-08-21.

## Product goal

Turn a Senior-approved content package into a traceable, brand-aware creative
workflow without locking the application to one design vendor.

```text
Client
  -> Brand Kit
  -> Content Package
  -> Senior Content Approval
  -> Design Brief
  -> Creative Studio
       -> Gemini AI generation / revision
       -> Canva workflow (future optional provider)
       -> Adobe / Photoshop finishing (future optional provider)
       -> Manual upload
  -> Immutable Creative Version
  -> Senior Design Review
       -> Approve
       -> Request Changes -> new creative version
  -> Publishing Gate
  -> Scheduling / Publishing
```

The approved marketing content remains immutable throughout creative production.
A design change never silently rewrites the content package.

## Phase implemented on this feature branch

### 1. Client Brand Kit

The Brand Kit is provider-neutral and versioned per client. It includes:

- brand name
- primary / secondary / accent colors
- heading and body font preferences
- brand voice
- visual style and preferred imagery
- website and Instagram handle
- Brand DO / DON'T rules
- notes
- optional PNG/JPEG logo metadata

Logo bytes remain in local/generated asset storage; SQLite stores bounded metadata,
path, SHA-256, MIME type, size, and immutable Brand Kit versions. The prompt layer
explicitly tells image models not to invent or redraw the client's logo.

### 2. Gemini text provider

The existing text-generation boundary continues to support Groq and n8n, and a
new router adds Gemini without changing the downstream calendar validators.

Recommended default text model for this phase:

- `gemini-3.7-flash`

Gemini requests use the Interactions API and set `store=false` for one-shot
campaign generation. Provider responses still pass through the application's
existing deterministic content parsing and validation before anything is saved.

### 3. In-app Gemini Creative Studio

The Creative Studio uses the exact approved post + saved Design Brief + latest
Brand Kit to construct a bounded image-generation prompt.

Recommended low-cost default:

- `gemini-3.1-flash-lite-image`
- 1K output
- 4:5 for Image/Carousel
- 9:16 for Reel/Video/Story

The user sees the generated image as a draft before explicitly saving it. Saving
creates a new immutable creative version and uses the same Senior Design Review
workflow as a manual upload.

### 4. Senior-feedback-driven image revision

When a creative is rejected, the revision prompt contains:

- the Senior-selected design change areas
- the Senior's exact feedback
- immutable approved marketing content
- current Brand Kit
- prior creative prompt as lower-priority context

If the rejected creative is an intact image, the previous creative bytes are sent
as a reference image so Gemini can revise rather than start blindly from scratch.
The prior prompt is clipped first if necessary; approved content and Senior
feedback are preserved within the 12,000-character application boundary.

### 5. Existing safeguards retained

- raw Senior review tokens are never stored
- every design review link is bound to the exact latest creative version/hash
- new creative versions revoke stale pending design-review links
- old decisions never carry over to a new creative version
- creative files are SHA-256 and size checked before review and publishing
- generated/manual creative binaries stay out of git
- Publishing remains locked until every latest required creative is approved

## Gemini developer API operational notes

This integration requires a Gemini Developer API key associated with a Google
Cloud / AI Studio project. API keys inherit their project's billing and quota.
Current Google documentation says new AI Studio keys are authorization keys, and
standard keys are scheduled to stop being accepted by the Gemini API in
September 2026, so use a newly created/recommended key rather than designing new
work around legacy unrestricted standard keys.

The image models used here currently do not offer image generation in the API
Free Tier. The official pricing page lists approximately:

- Gemini 3.1 Flash Lite Image, 1K: USD 0.0336 per generated image (standard)
- Gemini 3.1 Flash Image, 1K: USD 0.067 per generated image (standard)

The application deliberately generates one image per explicit click and has no
automatic cross-provider fallback. This reduces duplicate work/cost after an
ambiguous timeout. Use AI Studio project spend caps for an additional billing
control.

Official references:

- https://ai.google.dev/gemini-api/docs/get-started
- https://ai.google.dev/gemini-api/docs/api-key
- https://ai.google.dev/gemini-api/docs/billing
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/image-generation
- https://ai.google.dev/api/interactions-api-v1

## Canva: planned optional production provider

Do not make Canva a dependency of the core workflow. Canva Connect APIs can:

- create designs
- work with user designs
- create brand-template/design Autofill jobs
- poll asynchronous Autofill jobs
- export designs to formats such as PNG, JPG, PDF, GIF, PPTX, MP4 and CSV where
  the design type supports them

Autofill is not a generic public "Magic Design prompt" endpoint. It is a
structured template/design-data workflow. Current Canva documentation says
production Autofill must act on behalf of a user in a Canva Enterprise
organization; paid-plan users have a limited development trial. Therefore this
app should only enable the Canva Autofill provider when the account/integration
capabilities are actually available.

Planned Canva path:

```text
Approved Post + Brand Kit
  -> select mapped Canva template
  -> upload/choose generated visual asset
  -> Autofill approved text/image fields
  -> Canva design
  -> optional Edit-in-Canva handoff
  -> Export job
  -> downloaded export becomes a new creative version
  -> Senior Design Review
```

Official references:

- https://www.canva.dev/docs/connect/api-reference/designs/create-design/
- https://www.canva.dev/docs/connect/api-reference/autofills/
- https://www.canva.dev/docs/connect/autofill-guide/
- https://www.canva.dev/docs/connect/api-reference/exports/

## Adobe / Photoshop: planned premium finishing provider

Use Photoshop API **v2 only** for future production work. Adobe lists v2 as GA;
the legacy v1 API reached end of life on 2026-07-31. V2 supports production
operations such as Smart Objects, Actions, background removal, product crop,
layer/document workflows, text rendering and combined edit operations.

Photoshop API server-to-server access currently requires an Adobe Developer
Console project and an active Enterprise contract that includes Firefly Services,
with Client ID/Secret credentials. For that reason Adobe remains an optional
premium finishing provider rather than a prerequisite for the POC.

Planned Adobe path:

```text
Gemini/base visual or approved source asset
  -> Photoshop v2 template / Smart Object / Action pipeline
  -> real logo + controlled text layers + finishing
  -> exported final asset
  -> immutable creative version
  -> Senior Design Review
```

Official references:

- https://developer.adobe.com/firefly-services/docs/photoshop/
- https://developer.adobe.com/firefly-services/docs/photoshop/getting-started/
- https://developer.adobe.com/firefly-services/docs/photoshop/guides/photoshop-v2/

## Configuration

Keep real keys only in `.streamlit/secrets.toml` or server environment variables.
Never commit them.

Example Gemini configuration:

```toml
CALENDAR_GENERATION_PROVIDER = "gemini"
GEMINI_API_KEY = "replace_locally"
GEMINI_TEXT_MODEL = "gemini-3.7-flash"
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-lite-image"
```

Groq and n8n remain valid text providers. Gemini image generation can also be used
while the calendar text provider remains Groq or n8n.

## Rollout and validation order

1. Back up the real SQLite database.
2. Pull this feature branch.
3. Apply the one-command Brand Kit + Gemini Creative Studio transformation.
4. Compile all modified/new Python modules.
5. Run the complete pytest suite.
6. Test Brand Kit save/update and logo integrity.
7. Test a Gemini image draft, preview, save, and Senior approval.
8. Test a Senior rejection followed by Gemini reference-image revision (V1 -> V2).
9. Verify Publishing Gate remains locked until the latest V2 is approved.
10. Only after manual validation, commit transformed app/store source and prepare
    the feature PR. Do not merge based only on staging-script success.
