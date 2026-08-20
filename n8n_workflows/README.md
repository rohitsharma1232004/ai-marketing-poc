# n8n calendar gateway

`calendar_generate_v1.json` is the first versioned workflow in the marketing
automation system. It intentionally performs one job only:

1. Receive an authenticated calendar request from the Streamlit app.
2. Validate the request contract.
3. Call Groq with credentials stored in n8n.
4. Return the generated Markdown and the original request ID.

The Streamlit application continues to enforce the posting schedule, row count,
format mix, pillar mix, column names, and Excel-export rules.

## Import and configure

1. Open n8n and choose **Import from File**.
2. Import `calendar_generate_v1.json`.
3. Create a **Header Auth** credential named `Marketing App Webhook Secret`:
   - Name: `X-Webhook-Secret`
   - Value: a new long random value
4. Select that credential in the `Calendar Generate Webhook` node.
5. Create another **Header Auth** credential named `Groq Authorization`:
   - Name: `Authorization`
   - Value: `Bearer gsk_your_key_here` (there must be one space after `Bearer`)
6. Select that credential in the `Groq Chat Completion` node.
7. Save and activate the workflow.
8. Copy its **Production URL**, not its temporary test URL.

For a local Docker container whose port `5678` is published to Windows, the URL
will normally be:

```text
http://localhost:5678/webhook/v1/marketing/calendar/generate
```

Configure Streamlit with the same webhook secret:

```toml
CALENDAR_GENERATION_PROVIDER = "n8n"
N8N_CALENDAR_WEBHOOK_URL = "http://localhost:5678/webhook/v1/marketing/calendar/generate"
N8N_WEBHOOK_SECRET = "replace_with_the_same_random_value"
GROQ_MODEL = "openai/gpt-oss-120b"
```

When n8n and Streamlit are separate containers on the same Docker network, use
the n8n Compose service name instead of `localhost`, for example
`http://n8n:5678/webhook/v1/marketing/calendar/generate`.

## Safety notes

- Never put the Groq key in the workflow JSON or webhook request.
- Do not enable automatic fallback after an n8n timeout; the Groq request may
  already have completed.
- The current `Idempotency-Key` is a correlation key only. Persistent duplicate
  suppression will be added with the campaign database in Milestone 2.
- Use HTTPS before exposing the webhook outside the local machine.
- Successful n8n execution data contains client prompt text. Configure execution
  retention/pruning before using real client documents in production.
- This synchronous workflow is Milestone 1. Research, optional future approvals, publishing and analytics can be added as separate workflows later.
