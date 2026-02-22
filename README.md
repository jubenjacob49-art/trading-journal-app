# Trading Journal App

Local Streamlit app for:
- Trade logging and notes
- P&L metrics (win rate, total net, average net)
- Daily P&L calendar view
- Account management with account descriptions
- Journal filtering by account/symbol/tag
- Basic equity curve chart
- Landing page + loading screen
- Login and register pages (per-user data)
- Account deposits and withdrawals
- Account deletion (removes related trades/transfers)
- Optional image attachment per trade (with preview in Journal)
- Paste image from clipboard for trades (Ctrl+V)

## Run

```powershell
pip install -r requirements.txt
streamlit run app.py
```

The app stores data in `trading_journal.db` in this folder.

## Logo

To use your custom landing-page logo, save it as one of:
- `logo.png`
- `logo.jpg`
- `logo.jpeg`
- `assets/logo.png`
- `assets/logo.jpg`

## Free Hosting (Best Path)

### Option 1: Streamlit Community Cloud (recommended)

1. Push this project to a public or private GitHub repo.
2. Go to Streamlit Community Cloud: `https://share.streamlit.io/`
3. Click `Create app`.
4. Select repo/branch and set entrypoint to `app.py`.
5. Deploy and share the generated `*.streamlit.app` URL.

Important: this app currently uses local SQLite (`trading_journal.db`). On many free hosts, local disk is not guaranteed persistent. For reliable shared data, move DB to a hosted Postgres.

### Option 2: Render Free Web Service

1. Push code to GitHub.
2. Create a new Web Service on Render from your repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`

### Optional free database for shared persistent data

- Supabase (Postgres free plan)
- Neon (Serverless Postgres free plan)
