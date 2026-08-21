# Railway + Supabase deployment plan (POC)

This is the recommended zero/low-cost deployment path for the current single-instance POC.
It keeps the existing SQLite workflow instead of forcing a database rewrite before the demo.

## Architecture

- Railway web service: Streamlit app + optional in-process publishing scheduler
- Railway persistent volume mounted at `/data`: SQLite database + generated/brand/creative files
- Supabase Storage public bucket: exact Senior-approved publishable image bytes exposed to Meta by HTTPS
- Groq: content generation
- Gemini: creative generation
- Meta Graph API: Facebook Page + linked Instagram Professional publishing

## Why Railway instead of Streamlit Community Cloud for this POC

The application already uses SQLite and local creative files. Streamlit Community Cloud does not guarantee persistence of runtime local files. Railway Free currently provides limited volume storage, which lets this POC keep its existing SQLite architecture without a database migration. Check the Railway plan/credit limits before using it for real production traffic.

## 1. Apply local source transforms before final test

From the repository root:

```powershell
python staging/apply_meta_publishing_complete.py
python -m pytest -q
```

Do not commit `.streamlit/secrets.toml`, `data/`, or generated creative files.

## 2. Supabase Storage

Create a Supabase project, then create a Storage bucket named:

`publishing-media`

For this POC the bucket must be **public**, because Meta fetches image media from the URL during publishing. The app uploads objects using the server-side service-role key and verifies that the resulting public URL is readable before queueing a Meta job.

Server secrets/variables:

- `SUPABASE_URL=https://<project-ref>.supabase.co`
- `SUPABASE_SERVICE_ROLE_KEY=<server-side service-role key>`
- `SUPABASE_MEDIA_BUCKET=publishing-media`

Never put the service-role key in GitHub or client-side JavaScript.

## 3. Railway service

Create one Railway service from this GitHub repository. `railway.toml` contains the Streamlit start command and healthcheck.

Add a persistent volume and mount it at:

`/data`

Set these Railway variables:

- `CAMPAIGN_DB_PATH=/data/marketing_poc.sqlite3`
- `GENERATED_OUTPUT_DIR=/data/generated_outputs`
- `CREATIVE_OUTPUT_DIR=/data/generated_outputs/creative_assets`
- `BRAND_ASSET_DIR=/data/generated_outputs/brand_assets`
- `APP_PUBLIC_BASE_URL=https://<your-railway-domain>`
- `GROQ_API_KEY=<secret>`
- `GEMINI_API_KEY=<secret>`
- `CALENDAR_GENERATION_PROVIDER=groq`
- `GEMINI_IMAGE_MODEL=gemini-3.1-flash-lite-image`
- Supabase variables from section 2
- `META_GRAPH_API_VERSION=v25.0`

For scheduled publishing on the single-instance POC:

- `AUTO_PUBLISH_WORKER=true`
- `PUBLISHING_WORKER_INTERVAL_SECONDS=60`

If the Railway service is stopped/asleep/out of credit, scheduled jobs cannot run until the process is active again. `Publish now` does not have that delay because it dispatches during the active Streamlit request.

## 4. Meta test connection

For the current POC, the publishing UI stores a **credential reference only**, not the raw token.

Example connection form value:

`META_TOKEN_CLIENT_ABC`

Then configure the actual token only in Railway/server secrets:

`META_TOKEN_CLIENT_ABC=<client/page access token>`

Also save the client's Facebook Page ID and linked Instagram Professional account ID in the app's Meta Connection panel.

For Instagram publishing through the Facebook-login path, the account must be Professional and linked to the Page. The app will require the appropriate Meta publishing permissions/token during the test. External multi-client onboarding later requires the proper Meta App Review/Advanced Access flow rather than manually provisioning tokens.

## 5. Exact approval-to-publish safety

Publishing is allowed only when all of these match:

1. latest content version is Senior approved,
2. latest creative version is Senior Design Approved,
3. creative file hash still matches the approval,
4. client Meta connection belongs to the same client,
5. selected platform is present in approved content,
6. image format is platform compatible,
7. the exact approved image is uploaded to public media storage.

For Instagram Image posts, PNG/Gemini output is converted to JPEG **before** the creative version is saved for Senior Design Approval. No post-approval image conversion is allowed.

## 6. Current POC scope

End-to-end publishing implemented for:

- Facebook Page single-image posts: PNG or JPEG
- Instagram Professional single-image posts: JPEG
- Publish Now
- queued/scheduled jobs
- duplicate protection
- publication history/status
- ambiguous-timeout protection (`outcome_unknown` is never blindly retried)

Reel/Video and Carousel publishing remain intentionally blocked until real final MP4/multi-slide media asset pipelines exist. Caption/reel-script generation remains available, but the publisher will not pretend a single design image is a real Reel or Carousel.
