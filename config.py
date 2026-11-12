"""
FinSentry Global Configuration
Local, offline-first autonomous corporate financial auditing and reconciliation engine.
"""
import os
from pathlib import Path

# Base Paths (default to local workspace, configurable via env vars for VM/mounts like /mnt/vault_drop)
BASE_DIR = Path(__file__).resolve().parent
VAULT_DROP_DIR = Path(os.getenv("FINSENTRY_VAULT_DROP", str(BASE_DIR / "vault_drop")))
VAULT_OUTPUT_DIR = Path(os.getenv("FINSENTRY_VAULT_OUTPUT", str(BASE_DIR / "vault_output")))
PROCESSED_ARCHIVE_DIR = VAULT_OUTPUT_DIR / "processed_archive"
EXPORTS_DIR = VAULT_OUTPUT_DIR / "exports"
REPORTS_DIR = VAULT_OUTPUT_DIR / "reports"
DB_PATH = VAULT_OUTPUT_DIR / "finsentry.db"

# Ensure runtime directories exist
for p in [VAULT_DROP_DIR, VAULT_OUTPUT_DIR, PROCESSED_ARCHIVE_DIR, EXPORTS_DIR, REPORTS_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# Ollama / Gemma 4 Settings
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60.0"))

# Reconciliation Tolerances & Thresholds
AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "0.05"))
DATE_WINDOW_MIN_DAYS = int(os.getenv("DATE_WINDOW_MIN_DAYS", "-5"))
DATE_WINDOW_MAX_DAYS = int(os.getenv("DATE_WINDOW_MAX_DAYS", "30"))
VENDOR_SIMILARITY_THRESHOLD = float(os.getenv("VENDOR_SIMILARITY_THRESHOLD", "88.0"))

# Forensic Thresholds
PHANTOM_DEBIT_THRESHOLD = float(os.getenv("PHANTOM_DEBIT_THRESHOLD", "500.0"))
SMURFING_WINDOW_DAYS = int(os.getenv("SMURFING_WINDOW_DAYS", "14"))
SMURFING_SINGLE_MAX = float(os.getenv("SMURFING_SINGLE_MAX", "10000.0"))
SMURFING_AGGREGATE_MIN = float(os.getenv("SMURFING_AGGREGATE_MIN", "15000.0"))
