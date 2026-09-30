# Analytics Copilot

Upload CSV or Excel files, then ask questions in plain English. The app asks an
AI model to write pandas code, runs that code on your computer, and shows you
the answer with the tables and charts behind it.

- Reads every sheet of every Excel file, cleans common problems automatically
  (money and percentages stored as text, dates as text, title rows above the header).
- Works with **any** columns. Nothing about your data is hardcoded.
- Falls back to a backup model when one is rate-limited or misconfigured.
- Every answer shows the code that produced it, so you can check the work.

## What you need

- Python 3.10 or newer (tested on 3.12)
- An API key for at least one provider: Google Gemini, Groq, OpenAI or OpenRouter.
  Gemini and Groq both have free tiers.

## Setup (about 5 minutes)

**1. Open a terminal in the project folder**, then create a virtual environment.

```
python -m venv .venv
```

Activate it:

- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- Windows cmd: `.venv\Scripts\activate.bat`
- macOS / Linux: `source .venv/bin/activate`

**2. Install the packages**

```
pip install -r requirements.txt
```

**3. Create your settings file**

Copy `.env.example` to a new file named `.env`:

- Windows: `copy .env.example .env`
- macOS / Linux: `cp .env.example .env`

Open `.env` in a text editor and paste your key after the matching name, for
example `GEMINI_API_KEY=your-key-here`. Fill in only the providers you use.
Never share `.env` or paste your key into a chat. It is already in `.gitignore`.

**4. Check the model names**

`LLM_MODELS` lists models in the order they are tried, as `provider:model`.
Model names change over time, so confirm each one in your provider's model list
(Google AI Studio, the Groq console, and so on). A wrong name shows up in the
app as "model or address not found".

**5. Start the app**

```
streamlit run app.py
```

Your browser opens the app. If it does not, open the address shown in the terminal.

## Settings in `.env`

| Setting | What it does |
|---|---|
| `LLM_MODELS` | Models to try, in order, e.g. `gemini:model-a,gemini:model-b,groq:model-c` |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | One key per provider you use |
| `*_BASE_URL` | Provider addresses. Change only if your provider says so |
| `LLM_TEMPERATURE` | Lower gives more consistent answers (default 0.2) |
| `LLM_MAX_TOKENS` | Maximum length of a model reply (default 4000). Raise it if replies are cut off |
| `SAMPLE_ROWS` | Sample rows per table sent to the model (default 5). Set `0` to send none |
| `CODE_TIMEOUT_SECONDS` | Time limit for each analysis (default 30) |
| `CODE_FIX_RETRIES` | Times the model may repair failing code (default 1) |

After editing `.env`, click **Reload settings (.env)** in the sidebar. You do
not need to restart the app.

## Using the app

1. Upload files in the sidebar. Every Excel sheet becomes its own table.
2. Open **Your data** to check the preview, the column profile and the notes on
   what was cleaned automatically.
3. Ask a question. Follow-up questions such as "now split that by month" work,
   because the app remembers the conversation.
4. Under each answer, open **How this was calculated** to see the steps and code.
   Download the tables (Excel or CSV), charts (PNG) or the code.
5. **Clear chat** starts a new conversation on the same files. **End session**
   removes your files and the chat from the app.

## Try it with sample data (demo script, about 5 minutes)

1. Run `python make_mock_data.py`. It creates fake files in `sample_data/`
   (a `sample_data/` folder may already be included).
2. Upload `clients.xlsx`, `engagements.csv` and `invoices.xlsx` together.
3. Open **Your data**. Point out the cleaning notes: the text money and
   percentages in `engagements.csv` became numbers, and the title rows above the
   `invoices.xlsx` header were skipped. Point out the duplicate-row warnings.
4. Ask these in order:
   - "Give me a short overview of this data."
   - "Who are the top 5 clients by total spend?" (combines two files)
   - "Show total spend by service line as a bar chart."
   - "Now split that by year." (a follow-up)
   - "What is the average number of days to pay an invoice, by industry?" (combines two files)
   - "Which engagements belong to a client that is not in the client list?"
     (the data has 2 such rows on purpose)
5. Open **How this was calculated** on one answer to show the code, then click a
   download button.
6. Click **End session** to show that the data is cleared.

Answers depend on the model you use, so exact wording will differ.
The sample data is fake and safe to delete: remove `make_mock_data.py` and the
`sample_data/` folder. The app does not depend on them.

## What is sent to the AI model

Your files stay on your computer. The model receives:

- column names and summary statistics (types, missing values, min/max, common values)
- a few sample rows per table (`SAMPLE_ROWS`)
- your question and the chat so far
- the top rows of each result, so it can write the explanation

E-mail addresses and phone numbers are masked in what is sent. Other text, such
as names, is not. If your data is confidential, use a provider approved for it,
or set `SAMPLE_ROWS=0` to send no sample rows.

## Safety of the code runner

The model's code runs in a separate process with a time limit, without your API
keys, and with a safety check that blocks file access, network calls, system
commands and unusual imports. This protects against accidents and casual
misuse. It is **not a hardened sandbox**. Run the app on your own computer with
your own data, and do not put it online for untrusted users.

## Troubleshooting

| You see | What to do |
|---|---|
| "No API key found for any listed model" | Add a key to `.env`, save, click **Reload settings (.env)** |
| Model shows "model or address not found" | The model name in `LLM_MODELS` is wrong. Check the provider's list |
| Model shows "API key rejected" | Check the key in `.env` has no spaces or quotes and belongs to that provider |
| "rate limit reached" | Normal on free tiers. The app moves to the next model and retries later |
| "reply cut off: raise LLM_MAX_TOKENS" | Raise `LLM_MAX_TOKENS` in `.env`, then reload settings |
| "No model could answer right now" | Every model failed. The message lists why for each one |
| "took longer than N seconds" | Raise `CODE_TIMEOUT_SECONDS`, or ask a narrower question |
| "I could not get working code" | Rephrase, or name the columns you want used |
| `ModuleNotFoundError` | Activate the virtual environment and run `pip install -r requirements.txt` again |

## Known limits

- IDs made only of digits with leading zeros (for example `00123`) may lose the
  zeros when a CSV or Excel column is read as numbers. Alphanumeric IDs such as
  `C0123` are safe.
- Results are shown up to 2,000 rows. Larger tables are cut, and the screen says so.
- Charts are static images, not interactive.
- Very large files (hundreds of thousands of rows) work but are slower, because
  each analysis passes the tables to a separate process.

## Project files

| File | Job |
|---|---|
| `app.py` | Streamlit screen: upload, preview, chat, tables, charts, downloads, model status |
| `engine.py` | Settings, model connection and fallback, prompts, safe code runner |
| `data_loader.py` | Reads CSV and Excel, cleans, and builds the data profile |
| `make_mock_data.py` | Optional: creates fake sample files (safe to delete) |
| `requirements.txt` | Package list |
| `.env.example` | Settings template with no real keys |
| `.gitignore` | Keeps `.env` and data files out of Git |
| `README.md` | This file |

You create `.env` yourself from `.env.example`.
