# Analytics Copilot

Ask questions about your spreadsheets in plain English. Analytics Copilot reads your CSV and Excel files, has an AI model write the analysis code, runs that code on your computer, and returns a written summary with the tables and interactive charts behind it.

> **Status:** proof of concept. Built to validate the approach, not for production or multi-user use.

## Features

- **Any spreadsheet.** Reads every CSV file and every sheet of every Excel workbook. No column names are hardcoded.
- **Automatic cleaning.** Fixes title rows above the header, money and percentages stored as text, and dates stored as text. Every change is listed in a cleaning report.
- **Calculations you can verify.** The AI model writes pandas code, and Python computes the results on your full data. Each answer shows the code that produced it.
- **Tables and interactive charts.** Hover for exact values, zoom, pan, and toggle series. Download results as Excel.
- **Follow-up questions.** The conversation is remembered, so you can say "now split that by month".
- **Works with several AI providers.** Gemini (free tier available), Groq, OpenAI and OpenRouter. If one model is rate-limited or unavailable, the app switches to the next one automatically.
- **Privacy controls.** E-mail addresses and phone numbers are masked before anything is sent to the model, and you can limit or disable sample rows.

## How it works

1. **Load.** Your files are read in full on your computer and cleaned.
2. **Profile.** The app builds a description of each table: column names, types, summary statistics and a few sample rows.
3. **Plan.** The AI model receives that description and your question, and replies with pandas code.
4. **Run.** The code runs locally on the full data, in a separate process with a time limit and safety checks.
5. **Repair.** If the code fails, the model gets the error and one chance to correct it.
6. **Explain.** The model sees only the top rows of the result and writes the summary.

## Requirements

- Python 3.10 or newer
- An API key from at least one provider. Google Gemini has a free tier: [aistudio.google.com](https://aistudio.google.com) (sidebar: **Get API key**).

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

## Configuration

Settings are read from `.env` when the app starts. After editing `.env`, restart the app.

| Setting | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes, for Gemini | Your Google AI Studio key |
| `LLM_MODELS` | Yes | Models to try, in order, as `provider:model`. The first is the main model and the rest are backups. |
| `LLM_MAX_TOKENS` | No (4000) | Maximum length of a model reply. Raise it if answers are cut off. |
| `SAMPLE_ROWS` | No (5) | Sample rows per table shown to the model. Lower it to share less data. `0` sends none. |

Advanced settings, all optional with built-in defaults: `LLM_TEMPERATURE` (0.2), `CODE_TIMEOUT_SECONDS` (30), `CODE_FIX_RETRIES` (1), and, for other providers, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and the matching `*_BASE_URL` addresses.

Example:

```
GEMINI_API_KEY=your-key-here
LLM_MODELS=gemini:gemini-3.8-flash,gemini:gemini-3.5-flash,gemini:gemini-3.5-flash-lite
```

## Using the app

1. Upload one or more files in the sidebar. Every Excel sheet becomes its own table.
2. Open **Your data** to review the preview, the column profile and the cleaning notes.
3. Ask a question, or start from one of the suggested questions.
4. Open **How this was calculated** under an answer to see the steps and the code used.
5. Download the tables as **Excel**. Interactive charts have a download-as-image button in their toolbar.
6. **Clear chat** starts a new conversation on the same files. **End session & clear data** removes the files and the chat from the app.

## Demo with the sample data

The `sample_data/` folder contains fake clients, engagements and invoices. To recreate it, run `python make_mock_data.py`.

1. Upload `clients.xlsx`, `engagements.csv` and `invoices.xlsx` together.
2. Open **Your data** and review the cleaning notes: text amounts and percentages in `engagements.csv` became numbers, the title rows above the `invoices.xlsx` header were skipped, and duplicate rows were flagged.
3. Ask, in order:
   - "Give me a short overview of this data."
   - "Who are the top 5 clients by total spend?" (combines two files)
   - "Show total spend by service line as a bar chart."
   - "Now split that by year." (follow-up)
   - "What is the average number of days to pay an invoice, by industry?" (combines two files)
   - "Which engagements belong to a client that is not in the client list?" (the sample data contains 2 such rows, both for client `C9999`)
4. Open **How this was calculated** on one answer, then hover over a chart.
5. Click **End session & clear data**.

Wording and results depend on the model used. The sample data is fake and safe to delete.

## Privacy and data handling

Your files are read and analysed on your computer. All calculations run locally. Nothing is stored by the app after you end the session.

The AI provider receives:

- column names and summary statistics (types, missing values, minimum, maximum, common values)
- up to `SAMPLE_ROWS` sample rows per table
- your question and the earlier conversation
- the top rows of each result, so it can write the summary

The provider never receives the full files. E-mail addresses and phone numbers are masked in what is sent. Other text, such as names, is not.

Free API tiers may allow the provider to use submitted content under its own terms. Check them before using confidential data. For confidential data, use a provider approved by your organisation, or set `SAMPLE_ROWS=0`.

## Security of the code runner

The model's code runs in a separate process with a time limit and without your API keys. A safety check blocks file access, network calls, system commands and unapproved imports. This protects against accidents and casual misuse. **It is not a hardened sandbox.** Run the app on your own computer with your own data, and do not expose it to untrusted users.

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

## Known limitations

- IDs made only of digits with leading zeros (for example `00123`) can lose the zeros when read as numbers. Alphanumeric IDs such as `C0123` are safe.
- Results display up to 2,000 rows. Larger tables are cut, and the screen says so.
- An interactive chart carrying more than about 4 MB of data is rejected. The model is asked to summarise the data first.
- Charts must be assigned to top-level variables in the generated code to be displayed.
- Free-tier models are rate-limited, and code quality varies between models.
- Single user, running locally.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Streamlit interface: upload, data preview, chat, tables, charts, downloads |
| `engine.py` | Settings, model connection and fallback, prompts, and the code runner |
| `data_loader.py` | Reads and cleans CSV and Excel files and builds the data profile |
| `make_mock_data.py` | Optional: generates the fake sample files |
| `sample_data/` | Fake demo files (safe to delete) |
| `requirements.txt` | Python packages |
| `.env.example` | Settings template with no keys |
| `.gitignore` | Keeps `.env` and data files out of Git |

You create `.env` yourself from `.env.example`.

## Roadmap

- PowerPoint export of tables, charts and insights
- Handling tuned for Sprinklr, Traackr and ServiceNow exports
- Stronger isolation for code execution
- Support for an organisation-approved AI provider for confidential data
- Sign-in and multi-user support