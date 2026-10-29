#!/usr/bin/env bash
# Install the Python dependencies for the autoresearch harness.
# Idempotent: safe to run multiple times.
#
# Interpreter: PYTHON=python3.13 bash scripts/install_deps.sh (default python3.12).
# Scoring and profile building load models on an NVIDIA GPU (CUDA). Without
# one the install still completes and the CPU-safe test suite runs.

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3.12}"

if [ ! -d ".venv" ]; then
    echo "Creating venv with $PYTHON..."
    "$PYTHON" -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip --quiet

echo "Installing requirements.txt..."
pip install --quiet -r requirements.txt

echo "Installing the crawl4ai browser (only scripts/scrape_competitors.py needs it)..."
crawl4ai-setup >/dev/null 2>&1 || echo "  crawl4ai-setup failed; run it manually before scraping."

echo ""
echo "Verification:"
python -c "
pkgs = [
    ('sentence_transformers', 'embeddings'),
    ('bertopic', 'topic modeling'),
    ('gliner', 'zero-shot NER'),
    ('keybert', 'keyword extraction'),
    ('transformers', 'BART summarization'),
    ('networkx', 'entity graph'),
    ('rank_bm25', 'BM25 scoring'),
    ('textstat', 'readability'),
    ('dotenv', 'env var loading'),
    ('google.genai', 'Gemini API'),
    ('scipy', 'sigmoid for reranker'),
]
for p, purpose in pkgs:
    try:
        __import__(p)
        print(f'  OK  {p} ({purpose})')
    except Exception as e:
        print(f'  FAIL {p}: {e}')
"

echo ""
if [ ! -f ".env" ]; then
    echo "WARNING: .env not found. Copy .env.example and set GEMINI_API_KEY."
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    echo "NVIDIA GPU detected."
else
    echo "WARNING: no NVIDIA GPU detected. evaluate.py, eval_server.py and"
    echo "analyze_competitors.py load models on CUDA; only the CPU-safe tests will run."
fi

echo ""
echo "Running scripts/doctor.py (blank-state health check)..."
if python scripts/doctor.py --quick; then
    echo ""
    echo "Done. Harness dependencies + doctor.py green."
else
    rc=$?
    echo ""
    echo "doctor.py reported hard failures (exit $rc). Fix them, then re-run."
    exit $rc
fi
