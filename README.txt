AI Marketing Content POC — Single Senior Approval
==================================================

Current scope
-------------
Client details -> Groq or n8n generation -> validated content calendar -> Senior review -> Excel download.

Only ONE approval is active: Senior approval.
Client approval and WhatsApp/FastAPI review-link delivery are disabled in the active Streamlit app.
The older two-stage/WhatsApp implementation is preserved under legacy_approval_whatsapp/ for future reuse.

Run locally
-----------
1. Keep your existing .streamlit/secrets.toml with your own credentials.
2. Install dependencies if needed:

       pip install -r requirements.txt

3. Start the app from the project folder:

       streamlit run app.py

4. Fill the client form and click Generate Content Calendar.
5. Review the generated calendar on screen.
6. Senior enters name/email and either Approves or Requests Changes.
7. Excel download unlocks only after Senior approval.

Generation providers
--------------------
The existing generation provider setup is preserved:
- groq: direct Groq generation
- n8n: existing calendar_generate_v1.json workflow

Persistence
-----------
Generated calendars are stored in data/marketing_poc.sqlite3 and receive a Campaign ID.
A saved campaign can be reopened from Saved Calendar Lookup. The app checks the latest calendar version and its Senior approval before offering Excel.

Compatibility note
------------------
The SQLite schema still contains older client-approval/WhatsApp fields and statuses for backward compatibility. The active Streamlit UI does not request Client approval and does not send WhatsApp review links.

Security
--------
Do not commit or share .streamlit/secrets.toml. Keep API keys and webhook secrets private.
