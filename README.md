## Project summary

SNA Visualizer is a compact network-traffic analysis utility that connects to Cisco Secure Network Analytics (Stealthwatch/SMC), fetches flow records, enriches them with GeoIP data, and produces interactive visualizations (Sankey diagrams and world maps) plus CSV export. It is intended as a developer-centric tool for ad-hoc traffic inspection and visualization.

- Input: flow records from SMC (via API) or CSV files
- Enrichment: GeoIP lookup (optional; requires MaxMind license or local DB)
- Outputs: interactive HTML visualizations and CSV exports under `output/`
- UI: Gradio-based web UI for connection, filtering, and exporting

This project is focused on being a lightweight exploratory tool rather than a production data pipeline. It is configurable via environment variables in `.env` and keeps all sensitive keys out of source control.

## Quickstart

1. Create and activate a Python virtual environment (recommended):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill values before running the application.

3. Run the app:

```powershell
python app.py
```

4. Open the Gradio UI at the printed local URL (default `http://127.0.0.1:7860`) and run a search. Visualizations and CSV downloads will be saved under `output/` and reachable via file:// links.

## Environment variables

Key environment variables (see `.env.example` for examples)

## License

This project is provided under the MIT License. See `LICENSE` for details.

