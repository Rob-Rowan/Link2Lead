# 🔗 Link2Lead — B2B LinkedIn Lead Generator & Pipeline Tracker

A professional-grade Streamlit application that discovers high-value B2B prospects on LinkedIn using **Google X-Ray search** (powered by the [Serper.dev](https://serper.dev) API), then tracks them through a complete outreach pipeline — from discovery to connection — with full analytics and CSV export.

---

## ✨ Features

| Module | Description |
| --- | --- |
| **⚙️ Configuration & Presets** | Eight pre-built buyer persona search queries with optional location/keyword modifiers. |
| **🔍 Prospect Discovery Engine** | Executes Google X-Ray searches via Serper.dev, parses LinkedIn profiles, and flags leads already in your pipeline. |
| **📊 Pipeline Tracker** | Manage leads through `NEW → DM_SENT → CONNECTED` with skip/archive actions and persistent notes. |
| **📈 Analytics & CSV Export** | Live conversion metrics and one-click export of your full pipeline to CSV. |

### Built-in Buyer Personas

- Automation Agencies & No-Code Shops
- Boutique Software & App Dev Shops
- Fractional CTOs & Tech Advisors
- Law Firm Partners & Owners
- Medical Practices & Specialty Clinics
- Dental Groups & Specialty Dentistry
- Wealth Management & RIAs
- Logistics & Commercial Engineering

---

## 🚀 Quick Start (Windows 11)

### Prerequisites

- **Python 3.10+** — [Download from python.org](https://www.python.org/downloads/)
  - ⚠️ During installation, check **"Add Python to PATH"**.
- **Serper.dev API key** — see [Configuration](#-configuration) below.

### Option A — One-Click Launch (Recommended)

1. **Clone or download** this repository to your machine.
2. **Copy** `.env.example` to `.env` and add your Serper.dev API key (see [Configuration](#-configuration)).
3. **Double-click** `launch_link2lead.bat`.

The script will automatically:

1. Detect your Python installation.
2. Create a virtual environment (`.venv`) on first run.
3. Install all dependencies from `requirements.txt`.
4. Verify your `.env` configuration.
5. Launch the app and open it in your default browser.

> 💡 **First run** takes a few minutes while dependencies install. Subsequent launches are near-instant.

### Option B — Manual Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
copy .env.example .env
# ... edit .env and add your Serper.dev API key ...

# 4. Launch the app
streamlit run app.py
```

The app will be available at **http://localhost:8501**.

---

## ⚙️ Configuration

### 1. Serper.dev API Key

1. Go to [serper.dev](https://serper.dev) and create a free account.
2. Navigate to your **API Key** page in the dashboard.
3. Copy your API key (a long alphanumeric string).

### 2. Environment File

Create a `.env` file in the project root:

```env
SERPER_API_KEY=your_serper_api_key_here
DAILY_QUERY_LIMIT=95
```

| Variable | Description |
| --- | --- |
| `SERPER_API_KEY` | Your Serper.dev API key used to execute Google X-Ray searches. |
| `DAILY_QUERY_LIMIT` | Safety ceiling for daily API queries (default: `95`). |

> 🔒 The `.env` file is git-ignored and never committed. Use `.env.example` as your template.

---

## 🧭 How It Works

### The Workflow

```
Google X-Ray Search (via Serper.dev)
        │
        ▼
┌─────────────────────┐
│  Prospect Discovery │  Parses LinkedIn profiles from search results
│  Engine             │  and cross-references your existing pipeline
└─────────┬───────────┘
          │  Save / Skip
          ▼
┌─────────────────────┐
│  Pipeline Tracker   │  NEW → DM_SENT → CONNECTED
│                     │  (with skip & archive states)
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  Analytics & Export │  Conversion metrics + CSV download
└─────────────────────┘
```

### The SQLite Pipeline

All persistence is handled by a local SQLite database (`leads.db`, auto-created and git-ignored) with three tables:

| Table | Purpose |
| --- | --- |
| `leads` | Stores each discovered lead: canonical LinkedIn URL, name, headline, target category, location, pipeline status, timestamps, and notes. |
| `search_state` | Tracks the pagination page per query key so searches resume where they left off. |
| `daily_usage` | Records the number of API queries per calendar date to enforce the daily cap. |

**Pipeline mechanics:**

- **Canonicalization** — Every discovered LinkedIn URL is normalized to `https://www.linkedin.com/in/<handle>/`, stripping tracking parameters and regional subdomains. This guarantees deduplication.
- **Duplicate protection** — Before a lead is saved, its canonical URL is cross-referenced against the `leads` table. Existing leads are flagged in the UI with their current status.
- **Status flow** — Leads move through `NEW → DM_SENT → CONNECTED`, with `SKIPPED` and `ARCHIVED` as terminal states. Marking a lead as `DM_SENT` records the contact timestamp.
- **Daily quota** — Each Serper.dev API call increments the `daily_usage` counter for the current date. Once the `DAILY_QUERY_LIMIT` (default 95) is reached, searches are blocked until the next day.

### Pipeline Statuses

| Status | Meaning |
| --- | --- |
| `NEW` | Discovered and saved, no outreach yet. |
| `DM_SENT` | Direct message sent (timestamp recorded). |
| `CONNECTED` | Connection request accepted. |
| `SKIPPED` | Intentionally skipped (out of scope, duplicate, etc.). |
| `ARCHIVED` | Removed from the active pipeline. |

### Safety & Limits

- **Daily query cap** — Hard ceiling of 95 API calls per day (configurable via `DAILY_QUERY_LIMIT`).
- **Pagination cap** — Respects a maximum of 5 pages (50 results) per query.
- **Duplicate protection** — URLs are canonicalized and deduplicated automatically.
- **Usage telemetry** — Live quota meter in the app header.

---

## 🗂️ Project Structure

```
Link2Lead/
├── app.py              # Streamlit UI (4-tab dashboard)
├── config.py           # Environment loading, presets, constants
├── db.py               # SQLite persistence layer
├── search_engine.py    # Serper.dev search execution & parsing
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .gitignore          # Git ignore rules
├── launch_link2lead.bat # Windows 11 one-click launcher
└── leads.db            # SQLite database (auto-created, git-ignored)
```

### Architecture

The application follows a clean **layered architecture**:

| Layer | Module | Responsibility |
| --- | --- | --- |
| **UI** | `app.py` | Streamlit dashboard, user interaction, orchestration. |
| **Search** | `search_engine.py` | Serper.dev API calls, result parsing, dedup. |
| **Persistence** | `db.py` | SQLite storage: leads, search state, daily usage. |
| **Configuration** | `config.py` | Environment loading, presets, safety constants. |

---

## 🛠️ Development

### Running Tests

```bash
.venv\Scripts\activate
python -m pytest
```

### Code Style

The codebase follows PEP 8 with type hints throughout. Key conventions:

- `from __future__ import annotations` for modern type syntax.
- Google-style docstrings on all public functions.
- Constants centralized in `config.py`.

---

## 📄 License

This project is licensed under the **MIT License**. See the `LICENSE` file for details.

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/amazing-feature`).
3. Commit your changes (`git commit -m 'Add amazing feature'`).
4. Push to the branch (`git push origin feature/amazing-feature`).
5. Open a Pull Request.

---

## 📬 Support

- **Issues** — Report bugs or request features via [GitHub Issues](https://github.com/Rob-Rowan/Link2Lead/issues).
- **Repository** — [github.com/Rob-Rowan/Link2Lead](https://github.com/Rob-Rowan/Link2Lead)

---

*Built with ❤️ using [Streamlit](https://streamlit.io/), Python, and the [Serper.dev](https://serper.dev) API.*