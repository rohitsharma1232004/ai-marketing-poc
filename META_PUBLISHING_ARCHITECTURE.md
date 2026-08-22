# Meta Publishing Architecture

## Goal

Publish only the exact latest content + creative pair that passed both Senior gates.
No client social password is stored by this application.

## Connection strategy

For the agency use-case (Facebook Page + Instagram together), use Meta/Facebook Login
for Business and a Meta Business app. The connected Instagram account must be a
Professional account and, for the Facebook-Login Instagram path, linked to a Facebook
Page.

Expected permissions for the combined publishing product include:

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts` (Facebook Page publishing)
- `instagram_basic`
- `instagram_content_publish` (Instagram publishing)

When the product serves client accounts the app does not own/manage, plan for Meta
Advanced Access/App Review and the business-verification requirements that apply to the
requested permissions.

## Credential handling

`meta_connections` stores only a `credential_ref`, such as `META_TOKEN_CLIENT_ABC`.
The real access token belongs in environment/runtime secrets (and later a managed secret
store). Never store the raw token in SQLite, Git, campaign metadata, logs, or Streamlit
session state.

This abstraction is intentional: OAuth + a managed secret vault can replace the POC
runtime-secret resolver later without changing publication-job history.

## Publishing gate

A publication job is accepted only when all of the following are true:

1. Campaign has final Senior content approval.
2. Requested calendar version is the latest version.
3. Senior content approval matches the stored content hash.
4. Requested creative is the latest version for that post.
5. Senior Design Approval belongs to that creative ID and exact creative hash.
6. Every post in the approved campaign has a latest Senior Design Approved creative.
7. The requested platform is present in the approved post's Platform field.
8. The media type is supported by the current publishing phase.

## Phase 1 media support

Phase 1 intentionally supports only single-image PNG/JPEG posts.

Do **not** silently publish a Reel/Video row as a feed image and do **not** collapse a
Carousel into one image. Those actions would violate the approved format. Reel/Video
and Carousel publishing should be enabled only after the application stores real
platform-ready video/slide assets for the approved format.

## Instagram media delivery

Meta fetches Instagram publishing media from the URL supplied to the API. Therefore the
approved file must be available on a public HTTPS URL at publishing time. Local paths
such as `generated_outputs/...` are not sufficient.

For production, put approved assets in durable object storage/CDN and generate a URL
that remains valid for the scheduled publication window. Do not rely on a laptop or a
localhost URL.

## Scheduling

The application stores UTC `scheduled_for` values. A separate Python worker claims due
jobs atomically and publishes them. In production, run that worker from an always-on
server using cron/systemd/cloud scheduler. This keeps scheduling independent of whether
the Streamlit browser is open.

## Duplicate protection

Each exact campaign/content/creative/account/platform combination receives a stable
SHA-256 dedupe key. The database refuses a second publication job for the same approved
material.

A timeout during a final Meta publish call is treated as `outcome_unknown`, not a normal
failure. The worker does not auto-retry it because the post may already be live and a
blind retry can create a duplicate. An operator must first verify the destination
account, then resolve the job manually.

## Current direct endpoints

The provider client is version-pinned but configurable with `META_GRAPH_API_VERSION`.
The current code defaults to `v25.0`.

Single Facebook Page image:

- `POST /{page-id}/photos`

Single Instagram image:

- `POST /{ig-user-id}/media` (create container)
- `POST /{ig-user-id}/media_publish` (publish container)

## Next implementation layers

1. Wire the publishing queue into the post-design-approval Streamlit UI.
2. Add client OAuth onboarding instead of manual Page/IG IDs.
3. Add durable public object storage for approved assets.
4. Deploy the one-shot worker on an always-on service.
5. Add Reel/Video asset pipeline + container-status polling.
6. Add Carousel children + final carousel container flow.
7. Add platform post/permalink verification and analytics sync.
