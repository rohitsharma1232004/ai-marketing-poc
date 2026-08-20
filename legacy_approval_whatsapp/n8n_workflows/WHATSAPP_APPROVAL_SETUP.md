# WhatsApp review notification setup

This workflow sends a WhatsApp message, not an email. The message contains a
secure link to a review website where the senior or client can inspect the
calendar and explicitly approve or reject it.

Receiving or opening a WhatsApp message must not approve a calendar. After the
reviewer presses Approve in the review website, the application records the
decision automatically. A senior approval can then create the client's review
request; a client approval can move the exact calendar version to
fully_approved and unlock the final Excel export.

The importable adapter is whatsapp_review_notify_v1.json. It has six nodes:

    Authenticated Webhook -> Validate Contract -> Valid?
                                               -> WhatsApp -> Build response
                                               -> 400 response

## What this workflow does and does not do

It does:

- accept one authenticated, versioned notification request;
- reject unknown fields and malformed IDs, hashes, phone numbers, timestamps,
  roles, and token suffixes;
- send the fixed content_calendar_review_v1 / en_US template through n8n's
  official WhatsApp Business Cloud node;
- return the event ID, review-request ID, and Meta provider message ID.

It deliberately does not:

- generate or validate a signed review token;
- host the calendar or an approval form;
- accept a template name, message body, sender, or full URL from the caller;
- expose an n8n Wait/resume URL;
- decide or save an approval;
- create the final Excel file.

Those authority-sensitive operations belong in the review service (the planned
FastAPI/PostgreSQL boundary), not in a notification workflow.

## 1. Prepare Meta test assets

For the first development test, use the test assets in Meta for Developers >
your Business app > WhatsApp > API Setup:

1. Keep the Meta-provided test sending number selected.
2. Add and verify your own or the senior's WhatsApp number as a test recipient.
   Meta test setups normally allow up to five verified recipient numbers.
3. Keep the test access token private. It is temporary and is not the Groq key.
4. Copy the WhatsApp Business Account ID (WABA ID). Do not confuse it with the
   sender's Phone Number ID.

Meta changes dashboard labels periodically. Follow Meta's current
[Cloud API getting-started guide](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started)
if the menu wording differs.

For production, complete Meta business/phone verification, use a production
sending number and an appropriately scoped system-user token, and follow the
current WhatsApp Business policies. Obtain clear WhatsApp opt-in from each
recipient before sending business-initiated notifications. A phone number in
client data is not, by itself, proof of opt-in.

## 2. Create the one approved template

Create this template in WhatsApp Manager and wait until it is approved and
active:

| Setting | Required value |
| --- | --- |
| Name | content_calendar_review_v1 |
| Language | en_US |
| Intended category | Utility |
| URL button index | 0 |
| URL button label | Review calendar |

Use this body and preserve the variable order:

    Hello {{1}},

    A content calendar is ready for {{2}} review.

    Campaign ID: {{3}}
    Review by: {{4}}

    Open the calendar, then approve or reject it.

Suggested examples for Meta's template review are Priya, senior, a sample UUID,
and 2030-01-01T12:00:00Z.

Create one dynamic URL button:

    https://reviews.example.com/r/{{1}}

Replace reviews.example.com with the real public review-portal domain. The base
and /r/ path are fixed in the approved template. At send time, the n8n node
supplies only review_token_suffix; it does not supply the full URL.

| Meta variable | Request field |
| --- | --- |
| Body {{1}} | recipient_name |
| Body {{2}} | role (senior or client) |
| Body {{3}} | campaign_id |
| Body {{4}} | review_due_at |
| URL button {{1}}, index 0 | review_token_suffix |

Meta may reclassify or reject a template under its current category rules. If
wording must change, version the template and workflow together instead of
making the inbound template configurable. See Meta's
[message-template documentation](https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates/)
and [template-send guide](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates/).

## 3. Create the native n8n WhatsApp credential

In n8n, create a WhatsApp Business Cloud API credential:

- API Access Token: paste the raw Meta token, normally beginning with EAA.
  Do not add the word Bearer.
- Business Account ID: paste the WABA ID.

This is a native WhatsApp credential. Do not reuse the Groq Header Auth
credential, Groq API key, or review webhook secret. Follow n8n's current
[WhatsApp credential guide](https://docs.n8n.io/integrations/builtin/credentials/whatsapp/).

## 4. Import and configure

1. In n8n, choose Import from File and select
   whatsapp_review_notify_v1.json.
2. Create a separate Header Auth credential named
   Review Notification Webhook Secret:
   - Header name: X-Webhook-Secret
   - Header value: a new, long random value
3. Select it in WhatsApp Review Notify Webhook.
   The published production path is
   `/webhook/v1/marketing/reviews/whatsapp/notify`.
4. Open Send WhatsApp Review Template.
5. Select the native WhatsApp credential.
6. In Sender Phone Number (or ID), select the Meta test sender's Phone Number
   ID. The imported file intentionally leaves this blank.
7. Confirm the template is exactly content_calendar_review_v1 - en_US. Do not
   switch it to a caller-provided expression.
8. Save the workflow.

The JSON contains credential references only. It contains no API token, webhook
secret, sender number, recipient number, or n8n resume URL.

## 5. Test without sending

The safest first test is the invalid-contract branch:

1. Open the workflow and click Execute workflow so the test webhook listens.
2. Copy the Webhook node's Test URL.
3. POST an empty JSON object with the configured X-Webhook-Secret.
4. Confirm HTTP 400 with INVALID_REQUEST.
5. Confirm the WhatsApp node did not execute.

The repository structural test also parses the JSON and verifies the fixed
template, URL-suffix mapping, authentication, response contract, and absence of
embedded secrets.

For an isolated success mock, duplicate the workflow, keep the copy unpublished,
and replace only the WhatsApp node in the copy with a Code node that returns:

    return [{
      json: {
        messages: [{
          id: "wamid.mock-local-only",
          message_status: "accepted"
        }]
      }
    }];

Connect the mock node to Build Notification Response. Never publish or use the
mock copy as the production endpoint.

## 6. Send one end-to-end test

While Execute workflow is listening, use the Webhook node's Test URL and a
Meta-verified test recipient. Example PowerShell:

    $headers = @{
        "X-Webhook-Secret" = "replace-with-your-review-webhook-secret"
        "Content-Type" = "application/json"
    }

    $request = @{
        contract_version = "marketing.whatsapp-review-notification.v1"
        event_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
        review_request_id = "7cfd7815-3721-4313-89cd-578df44a2fe9"
        campaign_id = "73d8d783-973d-41bb-9a2d-bc1b98a56406"
        calendar_version_id = "a8651dd8-28a9-43c1-9766-5064b7385214"
        content_hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        role = "senior"
        recipient_name = "Rohit"
        recipient_phone_e164 = "+919876543210"
        review_due_at = "2030-01-01T12:00:00Z"
        review_token_suffix = "rv1.7cfd7815-3721-4313-89cd-578df44a2fe9.1893499200.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    } | ConvertTo-Json

    Invoke-RestMethod -Method Post -Uri "paste-the-n8n-test-url-here" -Headers $headers -Body $request

Replace the sample recipient with a verified number. Never put a real secret,
access token, or production review token in documentation or screenshots.

A successful synchronous response is HTTP 202:

    {
      "contract_version": "marketing.whatsapp-review-notification.v1",
      "ok": true,
      "event_id": "6ba7b810-9dad-41d1-80b4-00c04fd430c8",
      "review_request_id": "7cfd7815-3721-4313-89cd-578df44a2fe9",
      "status": "accepted",
      "provider": "whatsapp_cloud_api",
      "provider_message_id": "wamid..."
    }

Accepted means Meta accepted the send request. It does not prove delivery,
reading, identity, or approval. Delivery/read status is a later workflow using
signed Meta webhook events.

After the test succeeds, publish the workflow and use its Production URL. The
test URL works only while n8n is listening; the production URL works only for a
published workflow. See n8n's
[webhook development guide](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/workflow-development/).

## 7. Public review link requirement

The button must point to a public HTTPS review portal. It cannot point to
localhost, a Windows file path, the Streamlit LAN URL, the private n8n editor,
or $execution.resumeUrl.

For a temporary development demonstration, expose only the review application
through a controlled HTTPS tunnel and treat that URL as public. Production
should use a stable HTTPS domain, FastAPI behind a reverse proxy, authenticated
users or a short-lived one-time token, CSRF protection, rate limiting, and
PostgreSQL.

The review service should:

1. create a random, short-lived, single-use token and store only its hash;
2. bind it to recipient, role, campaign, version, content hash, and expiry;
3. send the complete rv1 token as the URL-button suffix to this workflow;
4. let GET /r/{token} verify the capability and redirect to the clean
   /review/{review_request_id} session URL without deciding;
5. accept an explicit approve/reject action only by POST;
6. recheck role, state, latest version, hash, expiry, and idempotency in one
   database transaction;
7. trigger the client notification only after senior approval commits;
8. unlock final Excel only after client approval commits as fully_approved.

Knowing a Campaign ID or receiving a WhatsApp message is not authentication.

## Request contract

The request permits exactly these fields; extra fields return HTTP 400:

| Field | Rule |
| --- | --- |
| contract_version | Exact: marketing.whatsapp-review-notification.v1 |
| event_id | UUID |
| review_request_id | UUID |
| campaign_id | UUID |
| calendar_version_id | UUID |
| content_hash | 64-character lowercase SHA-256 hex |
| role | senior or client |
| recipient_name | 1-120 characters; no control characters |
| recipient_phone_e164 | E.164 including the leading plus sign |
| review_due_at | ISO 8601 timestamp with Z or numeric offset |
| review_token_suffix | rv1.UUID.expiry.signature; bound to review_request_id and review_due_at |

There are no request fields for message, template, language, full URL, sender,
credential, or API token.

## Production safety checklist

- Keep the n8n editor private and expose only the authenticated webhook.
- Use a different secret from the calendar-generation webhook.
- Allow only the review service to call this webhook where possible.
- Deduplicate by event_id/review_request_id in the application outbox. n8n and
  Meta do not provide exactly-once business processing.
- Retry network failures, HTTP 429, and 5xx with bounded backoff. Do not retry a
  400 validation error unchanged.
- Use short-lived review links. Rotate an expired link rather than extending it.
- Collect evidence of recipient opt-in and honor opt-out requests.
- Limit access to n8n credentials and execution history.
- Configure execution retention/pruning. Requests contain a phone number and
  live review-token suffix, and n8n execution data may persist both. See
  [n8n executions](https://docs.n8n.io/workflows/executions/all-executions/)
  and the [n8n security audit](https://docs.n8n.io/hosting/securing/security-audit/).
- Do not put uploaded documents, calendar content, API keys, or full signed
  URLs in this contract.

## n8n version compatibility

The JSON targets Webhook 2.1, Code 2, If 2.2, WhatsApp Business Cloud 1.1, and
Respond to Webhook 1.5.

The exact self-hosted Docker/n8n version was not available when this file was
created. n8n can change import serialization and node option schemas. If the
workflow imports but the WhatsApp template or sender selector does not resolve,
update n8n and reselect those two values. On an older supported version,
recreate only the WhatsApp node using the official node, preserve the mappings
above, and reconnect it between Request Valid? and Build Notification Response.
Do not replace it with an HTTP Request node containing a hand-written Meta
bearer token.

Official references:

- [n8n WhatsApp Business Cloud node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.whatsapp/)
- [n8n WhatsApp credentials](https://docs.n8n.io/integrations/builtin/credentials/whatsapp/)
- [n8n Webhook node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)
- [Meta WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/)
- [Meta message templates](https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates/)
- [WhatsApp Business Messaging Policy](https://www.whatsapp.com/legal/business-messaging-policy)
