# MedLens

MedLens is a responsive personal workspace for organizing laboratory reports. It helps you review and correct extracted values, find reports, and compare historical results when units match. It is an organizer, not a diagnostic or treatment tool.

## What it includes

- Upload PDF, PNG, JPG, and JPEG files up to 15 MB. The server checks file signatures and computes SHA-256 so an identical report cannot be saved twice.
- Extract text from digital PDFs. The app can attempt OCR when Tesseract and its supporting system tools are installed; extraction is a suggestion and may be incomplete.
- Review, edit, remove, or manually enter tests, values, units, and printed reference ranges before saving.
- Browse, search, and date-filter reports. See result history only for matching units; no unit conversion is done.
- Flag values only against the range printed on the same report.
- See local plain-language summaries in English, Hindi, and Spanish.
- Optionally request a Bedrock summary after a separate consent prompt. The feature is disabled unless AWS credentials and a model are configured.
- Create appointment and follow-up reminders; download CSV and JSON; print a report summary or save it as PDF from the browser.
- Sign into the password-protected workspace and delete one report or all workspace data.

## Requirements

- Python 3.12 or newer (Vercel's current FastAPI guide uses Python 3.12+)
- A MongoDB Atlas database and connection string
- A long random `SESSION_SECRET` and a strong workspace password

## Run locally

1. Download or clone this project and open a terminal in its folder.
2. Create and activate a virtual environment:

   **Windows PowerShell**

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   **macOS / Linux**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies: `python -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`, then fill in `MONGODB_URI`, `SESSION_SECRET`, and `WORKSPACE_PASSWORD`. Keep `.env` private. In PowerShell, run `Copy-Item .env.example .env`.
5. Start the app. It reads `.env` automatically:

   ```bash
   uvicorn main:app --reload
   ```

6. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with your workspace password.

For local OCR, install `requirements-ocr.txt` plus the Tesseract OCR executable and Poppler, then ensure their executables are on your system PATH. OCR is optional; you can always add results manually.

### Creating a secure password hash (optional)

You can store a scrypt password hash in `WORKSPACE_PASSWORD_HASH` instead of the plain password setting. With the virtual environment active, run:

```bash
python -c "import getpass; from main import password_hash; print(password_hash(getpass.getpass('Workspace password: ')))"
```

Save the output as `WORKSPACE_PASSWORD_HASH`, remove `WORKSPACE_PASSWORD`, and keep the hash private too. Do not put secrets in source code or commit them.

## MongoDB Atlas setup

1. Create a MongoDB Atlas cluster and database user.
2. Add the deployment's outbound network access according to your Atlas network-access policy. Atlas IP allowlists and Vercel's serverless outbound IP behavior need to be considered; a broad `0.0.0.0/0` allow rule is convenient for a demo but weakens network restrictions. Prefer a private networking option or managed egress/IP solution for production.
3. Copy the application's connection string and set `MONGODB_URI`. URL-encode special characters in the database username or password.
4. Set `MONGODB_DATABASE` to `medlens` (or another database name). The app creates report indexes when it first connects.

The current app provides one shared password-protected workspace. Anyone with that password can access the same reports. It is not a multi-user account system.

## Deploy to Vercel

1. Push this project to a Git provider supported by Vercel, then import the repository in Vercel.
2. Use the Python project runtime. Vercel detects the FastAPI application in `main.py`; `vercel.json` includes the HTML frontend with the function.
3. In **Project Settings → Environment Variables**, add:
   - `MONGODB_URI`
   - `MONGODB_DATABASE` (for example, `medlens`)
   - `SESSION_SECRET` (a unique random value of at least 32 characters)
   - `WORKSPACE_PASSWORD` **or** `WORKSPACE_PASSWORD_HASH`
4. Redeploy after setting variables, then visit the assigned Vercel domain and sign in.
5. Keep secrets out of Git, previews, logs, and screenshots. Set production and preview secrets intentionally; a preview deployment with production database credentials has access to those records.

The app sends files to the server in 3 MB chunks because Vercel Functions cap a single request body at 4.5 MB; the user-facing total file limit remains 15 MB. Temporary chunks are stored in MongoDB and expire after an hour if interrupted, or are deleted as soon as the file is processed. Vercel function limits and cold starts apply. The project configuration is prepared for Vercel, but this source project is not deployed or live until you connect the database, set environment variables, and deploy it from your Vercel account.

## Optional Amazon Bedrock summaries

Bedrock is off unless `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `BEDROCK_MODEL_ID` are set. Optionally set `AWS_REGION`. Configure AWS permissions for only the required Bedrock model and region. When a user requests a summary, the app shows an explicit confirmation; after consent, only the selected report's verified result values are sent to Bedrock. The source PDF, report title, lab, source text, and reminders are not sent by this feature. Review AWS and model-provider data handling terms before enabling it.

## Privacy and safety

- Lab reports are sensitive health data. This starter app is not certified or represented as HIPAA-compliant or suitable for regulated clinical records. Review applicable privacy, security, and health-data rules before real-world use.
- Only extracted text and user-verified report values are retained as report records. Uploads are sent in 3 MB chunks through MongoDB to stay below Vercel's request-body limit; temporary chunk records expire after one hour and are deleted after processing. Original file bytes are not retained as report data.
- The single workspace password is shared by all users of this deployment. Use a strong secret, restrict who can access the deployment, and do not use it for high-risk clinical workflows.
- Use HTTPS in production (Vercel provides it). The session cookie is HTTP-only, SameSite Strict, and Secure in Vercel production.
- Add backups and test restore procedures in Atlas. JSON export is available in the dashboard.
- Parsing is heuristic and may miss, misread, or incorrectly associate a result or range. Confidence is a rough indicator, not a probability. Verify every entry against the original report before saving.
- OCR requires system binaries not bundled in Vercel's standard serverless runtime. In Vercel, scanned reports may have no extracted text; manual entry remains available. For reliable scanned-report support, connect a separately secured OCR service and assess its privacy terms.
- Range flags use only the printed report-specific numeric range. They do not interpret qualitative results and do not account for clinical context.
- Trend charts exclude nonnumeric values and do not compare or convert different units.
- Browser print creates a printable report summary; choose “Save as PDF” in the browser's print dialog to create a PDF.
- Delete-all permanently deletes reports and reminders from the app's MongoDB collections for this workspace. It does not delete database backups or copies you exported.

## Project layout

```text
main.py               FastAPI app, Vercel entry point, extraction and API
static/index.html     Responsive frontend
requirements.txt      Core Python dependencies
requirements-ocr.txt  Optional OCR Python dependencies
vercel.json           Vercel routing and function configuration
.env.example          Safe environment-variable template
```
