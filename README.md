# Analytics Copilot

Two tools in one Streamlit app:

1. **Ask your data (main page).** Upload any CSV or Excel files and ask questions in plain English. An AI model writes the analysis code, Python runs it locally on your full data, and you get a written summary with tables and interactive charts.
2. **KPI Dashboard (`Dashboard` page).** Upload ServiceNow, Sprinklr and Traackr exports, save fixed-formula KPIs for each month, compare periods, read automatic insights, and download a PowerPoint report. No AI is needed for the numbers.

An optional third page, **LLM Insights**, shows every AI call the app has made (model, tokens, cost, latency) using Langfuse.


## Features

### Ask your data
- **Any spreadsheet.** Reads every CSV file and every sheet of every Excel workbook. No column names are hardcoded.
- **Automatic cleaning.** Fixes title rows above the header, money and percentages stored as text, and dates stored as text. Every change is listed in a cleaning report.
- **Calculations you can verify.** The AI model writes pandas code and Python computes the results on your full data.
- **Tables and interactive charts.** Hover for exact values, zoom, pan and toggle series. Download all result tables as one Excel file, and static charts as PNG.
- **Follow-up questions.** The conversation is remembered, so you can say "now split that by month".
- **Several AI providers.** Gemini (free tier available), Groq, OpenAI and OpenRouter. If one model is rate-limited or unavailable, the app switches to the next one automatically.
- **Privacy controls.** E-mail addresses and phone numbers are masked before anything is sent to the model, and you can limit or disable sample rows.

### KPI Dashboard
- **Source detection.** The app recognises ServiceNow, Sprinklr and Traackr workbooks from their column names, finds the real header row on each sheet, and reads only the sheets and columns it needs.
- **Period confirmation.** The reporting period is detected from the file (latest month for ServiceNow, the title text for Sprinklr, the campaign dates for Traackr). You confirm or correct it before saving.
- **Fixed KPI formulas.** Every KPI is defined in code in `kpi_config.py`. The AI never calculates a number.
- **Saved snapshots.** Only calculated results are saved, never raw rows. Saving the same source and period again creates a new version and keeps the old one.
- **Period comparison.** Pick a period and compare it with another saved one. Percentage KPIs change in percentage points.
- **Charts.** Cards for single numbers, and bar or donut charts for breakdowns. The chart type is chosen from the shape of the data. A trend line shows a KPI across all saved periods.
- **Insights and recommendations.** Plain-language sentences generated from the saved numbers by rules in `insights.py`. Every number comes from the snapshot.
- **PowerPoint report.** One click builds a deck from the saved KPIs, charts, insights and recommendations, using the template in `templates/`.

### LLM Insights
- Cards for number of AI calls, total tokens, total cost and average latency, plus a table of the latest calls.
- Choose the last 7 days, the last 30 days or all time. The page refreshes every 15 seconds.
- Needs Langfuse keys. Without them the page shows a setup message and the rest of the app works normally.

## How it works

### Ask your data
1. **Load.** Your files are read in full on your computer and cleaned.
2. **Profile.** The app builds a description of each table: column names, types, summary statistics and a few sample rows.
3. **Plan.** The AI model receives that description and your question, and replies with pandas code.
4. **Run.** The code runs locally on the full data, in a separate process with a time limit and safety checks.
5. **Repair.** If the code fails, the model gets the error and a chance to correct it (`CODE_FIX_RETRIES`).
6. **Explain.** The model sees only the top rows of the result and writes the summary.

### KPI Dashboard
1. **Read.** `sources.py` identifies the source, maps real column names to common names and detects the period.
2. **Calculate.** `kpi_engine.py` applies the fixed formulas from `kpi_config.py` to the rows inside the confirmed period.
3. **Save.** `snapshot_store.py` stores the calculated snapshot.
4. **Compare.** `comparison.py` calculates the change between two saved periods.
5. **Explain.** `insights.py` writes insights and recommendations from the saved numbers.
6. **Report.** `report_deck.py` prepares the slides and `pptx_writer.py` draws the PowerPoint.

## Requirements

- Python 3.10 or newer
- An API key from at least one provider for the main page. Google Gemini has a free tier: [aistudio.google.com](https://aistudio.google.com) (sidebar: **Get API key**).
- The KPI Dashboard works without any AI key.

## Setup

**1. Create a virtual environment** in the project folder.

```
python -m venv .venv
```

Activate it:

- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- Windows cmd: `.venv\Scripts\activate.bat`
- macOS / Linux: `source .venv/bin/activate`

**2. Install the packages.**

```
pip install -r requirements.txt
```

**3. Create your settings file.** Copy `.env.example` to `.env`:

- Windows: `copy .env.example .env`
- macOS / Linux: `cp .env.example .env`

Open `.env` and paste your key after `GEMINI_API_KEY=`. Never share `.env` or commit it. It is already listed in `.gitignore`.

**4. Check the model names.** Model names change over time. Confirm each entry in `LLM_MODELS` against your provider's model list (for Gemini, Google AI Studio). A wrong name appears in the app as "model or address not found".

**5. Start the app.**

```
streamlit run app.py
```

The main page opens first. Use the page menu in the sidebar to open **Dashboard** and **LLM Insights**.

### Run in a dev container or Codespaces

The `.devcontainer/` folder starts the app automatically on port 8501 using Python 3.11. Add your keys to a `.env` file in the container before asking questions.

## Configuration

Settings are read from `.env` when the app starts. After editing `.env`, restart the app.

| Setting | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes, for Gemini | Your Google AI Studio key |
| `LLM_MODELS` | Yes | Models to try, in order, as `provider:model`. The first is the main model and the rest are backups. |
| `LLM_MAX_TOKENS` | No (4000) | Maximum length of a model reply (256 to 32000). Raise it if answers are cut off. |
| `SAMPLE_ROWS` | No (5) | Sample rows per table shown to the model (0 to 20). `0` sends none. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | No | Turns on AI-call logging and the LLM Insights page. Leave empty to switch it off. |
| `LANGFUSE_BASE_URL` | No | Langfuse address (default `https://cloud.langfuse.com`). `LANGFUSE_HOST` also works. |
| `STORE_BACKEND` | No (`memory`) | Where KPI snapshots are kept: `memory` or `sqlite`. See below. |
| `SNAPSHOT_DB` | No (`snapshots.db`) | File name used when `STORE_BACKEND=sqlite`. |
| `INSIGHTS_TIMEZONE` | No (`Asia/Kolkata`) | Time zone shown on the LLM Insights page. |

Advanced settings, all optional with built-in defaults: `LLM_TEMPERATURE` (0.2), `CODE_TIMEOUT_SECONDS` (30), `CODE_FIX_RETRIES` (1), and, for other providers, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and the matching `*_BASE_URL` addresses.

Example:

```
GEMINI_API_KEY=your-key-here
LLM_MODELS=gemini:gemini-3.8-flash,gemini:gemini-3.6-flash,gemini:gemini-3.5-flash-lite
```

### Where KPI snapshots are stored

- `memory` (default): snapshots are shared by every browser tab and **lost when the server restarts**.
- `sqlite`: snapshots are saved to a local file and survive restarts. Set `STORE_BACKEND=sqlite` in `.env`. On a hosted app such as Streamlit Cloud, local files are wiped on redeploy, so SQLite only persists when you run the app on your own machine.

## Using the main page

1. Upload one or more files in the sidebar and click **Start session**. Every Excel sheet becomes its own table.
2. Open **Your data** to review the preview, the column profile, the cleaning notes and the columns shared between tables.
3. Ask a question, or start from one of the suggested questions.
4. Download all result tables as **Excel**, and static charts as **PNG**. Interactive charts also have a download-as-image button in their toolbar.
5. **Clear chat** starts a new conversation on the same files. **End session & clear data** removes the files and the chat from the app.

## Using the KPI Dashboard

1. Open the **Dashboard** page and expand **Upload files**.
2. Select one or more Excel exports (`.xlsx`, `.xlsm`, `.xls`). The page shows the source it recognised, the sheets it used and how many rows it read.
3. Confirm the **period start and end** for each file.
4. Click **Calculate and save**. Any problems with a file or period are shown and nothing empty is saved.
5. Choose a **Period** and, optionally, another period under **Compare with**. Only saved periods are offered.
6. Read the KPI cards, the insights and recommendations, the breakdown charts, the top-10 table and the trend across saved periods for each source.
7. Click **Generate report (PPT)**, then download the `.pptx` file.

### Supported sources

| Source | Tables read | Period | Example KPIs |
|---|---|---|---|
| ServiceNow | Raw tasks, requests, request details | Latest month in the file. Rows outside the chosen period are removed. | Total tasks, completion rate, cancelled tasks, average business duration (hours), tasks by SLA type, request type, region, country and priority |
| Sprinklr | Organic posts, paid ads | Read from the title text of the file (`From: ... To: ...`) | Total engagements, engagements by account type, total ad spend, paid engagements, paid impressions, CPM |
| Traackr | Brands, all markets, campaigns | Campaign start dates | Posts, engagements, estimated impressions, video views, engagement rate, brand scores (vitality, visibility, impact, trust), top brands, campaign count and spend |

For Sprinklr and Traackr, the numbers cover the whole file. Changing the dates only changes the label.

> **Note:** the KPI formulas are currently **placeholders** (`KPI_VERSION = "0.3-placeholder"`) that still need to be confirmed with the business. When you change a formula in `kpi_config.py`, change `KPI_VERSION` so older snapshots stay traceable.

### Adapting to a new export format

All recognition rules live at the top of `sources.py` (the `SOURCES` block): which columns identify a sheet, how real column names map to common names, and how the period is detected. KPI formulas live in `kpi_config.py`. Report wording and layout live in `report_deck.py` and `pptx_writer.py`, and the slide template is `templates/diageo_template.pptx`.

## Demo with the sample data

The `sample_data/` folder is not included in the repository. Create it with:

```
python make_mock_data.py
```

It contains fake clients, engagements and invoices. This demo is for the main page.

1. Upload `clients.xlsx`, `engagements.csv` and `invoices.xlsx` together and click **Start session**.
2. Open **Your data** and review the cleaning notes: text amounts and percentages in `engagements.csv` became numbers, the title rows above the `invoices.xlsx` header were skipped, and duplicate rows were flagged.
3. Ask, in order:
   - "Give me a short overview of this data."
   - "Who are the top 5 clients by total spend?" (combines two files)
   - "Show total spend by service line as a bar chart."
   - "Now split that by year." (follow-up)
   - "What is the average number of days to pay an invoice, by industry?" (combines two files)
   - "Which engagements belong to a client that is not in the client list?" (the sample data contains 2 such rows, both for client `C9999`)
4. Hover over a chart, then click **End session & clear data**.

Wording and results depend on the model used. The sample data is fake and safe to delete. The KPI Dashboard needs real ServiceNow, Sprinklr or Traackr exports, because it recognises them by their column names.

## Privacy and data handling

Your files are read and analysed on your computer. All calculations run locally. Nothing is stored by the main page after you end the session.

**What the AI provider receives (main page):**

- column names and summary statistics (types, missing values, minimum, maximum, common values)
- up to `SAMPLE_ROWS` sample rows per table
- your question and the earlier conversation
- the top rows of each result, so it can write the summary

The provider never receives the full files. E-mail addresses and phone numbers are masked in what is sent. Other text, such as names, is not.

**KPI Dashboard:** KPIs are calculated without AI. Saved snapshots contain calculated results only, never raw rows.

**Langfuse (optional):** when keys are set, the app records metadata for each AI call: step name, model, provider, token counts, latency, cost and status, plus a session id. It does not send prompts, sample rows or answers.

Free API tiers may allow the provider to use submitted content under its own terms. Check them before using confidential data. For confidential data, use a provider approved by your organisation, or set `SAMPLE_ROWS=0`.

## Security of the code runner

The model's code runs in a separate process with a time limit and without your API keys. A safety check blocks file access, network calls, system commands and unapproved imports. This protects against accidents and casual misuse. **It is not a hardened sandbox.** Run the app on your own computer with your own data, and do not expose it to untrusted users.

**LLM Insights page:** the password gate in `pages/llm_insights.py` (`INSIGHTS_PASSWORD`) is currently commented out, so anyone who can open the app can see this page. Re-enable it before sharing the app with other people.

## Troubleshooting

| You see | What to do |
|---|---|
| "No API key found for any listed model" | Add a key to `.env`, save, and restart the app |
| "AI engine unavailable" in the sidebar | No listed model has a key. Check `.env` and restart |
| "model or address not found" | The model name in `LLM_MODELS` is wrong. Check the provider's model list |
| "API key rejected" | Check the key has no spaces or quotes and belongs to that provider |
| "rate limit reached" | Normal on free tiers. The app moves to the next model and retries later |
| "reply cut off: raise LLM_MAX_TOKENS" | Raise `LLM_MAX_TOKENS` in `.env` and restart |
| "No model could answer right now" | Every model failed. The message lists the reason for each |
| "took longer than N seconds" | Ask a narrower question, or raise `CODE_TIMEOUT_SECONDS` |
| "I could not get working code" | Rephrase, or name the columns to use |
| `ModuleNotFoundError` | Activate the virtual environment and run `pip install -r requirements.txt` again |
| Dashboard says a file is not recognised | The sheet column names do not match the signatures in `sources.py`. The message lists what was expected |
| Saved KPIs disappear after a restart | `STORE_BACKEND` is `memory`. Set it to `sqlite` |
| LLM Insights says Langfuse is not set up | Add `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to `.env` and restart |

## Known limitations

- IDs made only of digits with leading zeros (for example `00123`) can lose the zeros when read as numbers. Alphanumeric IDs such as `C0123` are safe.
- Results display up to 2,000 rows. Larger tables are cut, and the screen says so.
- An answer shows up to 4 charts. An interactive chart carrying more than about 4 MB of data is rejected, and the model is asked to summarise the data first.
- Charts must be assigned to top-level variables in the generated code to be displayed.
- Free-tier models are rate-limited, and code quality varies between models.
- The KPI Dashboard only understands the three supported export layouts, and its KPI formulas are placeholders until confirmed.
- Insights on the Dashboard use plain rule-based sentences. The option to have AI reword them is switched off in the current page.
- Sheets are limited to the first 100,000 rows when read by the KPI Dashboard.
- Single user, running locally.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Main page: upload, data preview, chat, tables, charts, downloads |
| `pages/dashboard.py` | KPI Dashboard: upload, period confirmation, KPI cards, charts, comparison, PowerPoint report |
| `pages/llm_insights.py` | LLM Insights: table and totals of every AI call, read from Langfuse |
| `engine.py` | Settings, model connection and fallback, prompts, and the code runner |
| `data_loader.py` | Reads and cleans CSV and Excel files and builds the data profile |
| `sources.py` | Recognises ServiceNow, Sprinklr and Traackr files, maps columns, detects and applies the period |
| `kpi_config.py` | The KPI catalogue: names, formulas, units and which table each reads |
| `kpi_engine.py` | Calculates a snapshot of KPIs and chart data from a recognised file |
| `snapshot_store.py` | Saves and loads KPI snapshots (memory or SQLite), with versioning |
| `comparison.py` | Change between two snapshots and trend series |
| `insights.py` | Insights and recommendations from saved KPIs |
| `report_deck.py` | Turns saved KPIs and insights into the slide plan for the report |
| `pptx_writer.py` | Draws the PowerPoint deck on the template, with native editable charts and tables |
| `templates/diageo_template.pptx` | Slide template used by the report |
| `observability.py` | Langfuse logging and reading of AI-call metadata |
| `report_engine.py`, `report_prompts.py` | AI-planned report builder and its wording. Used only by the disabled report page |
| `disabled_pages/create_report.py` | Earlier "create report from uploaded files" page. Move it into `pages/` to turn it back on |
| `make_mock_data.py` | Optional: generates the fake sample files |
| `requirements.txt` | Python packages |
| `.env.example` | Settings template with no keys |
| `.gitignore` | Keeps `.env` and data files out of Git. Add `snapshots.db` if you use the SQLite backend |
| `.devcontainer/` | Dev container and Codespaces setup |

You create `.env` yourself from `.env.example`.

## Roadmap

- Confirm the KPI definitions with the business and replace the placeholder formulas
- Re-enable the password on the LLM Insights page
- A shared database backend for KPI snapshots (for example Postgres) so they survive hosted redeploys
- Optional AI rewording of dashboard insights, with the existing number checks
- Re-enable AI-planned reports from any uploaded file
- Stronger isolation for code execution
- Support for an organisation-approved AI provider for confidential data
- Sign-in and multi-user support