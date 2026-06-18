#!/bin/bash
# Launch the Streamlit app locally.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] Virtual environment not found."
    echo "  python -m venv .venv"
    echo "  .venv/bin/pip install -r requirements.txt"
    exit 1
fi

.venv/bin/python -m streamlit run app/app.py \
    --server.enableXsrfProtection false \
    --server.enableCORS false \
    --browser.gatherUsageStats=false
