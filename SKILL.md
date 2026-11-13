---
name: finsentry-audit-engine
description: Operate, maintain, and extend FinSentry — the local autonomous corporate financial auditing, bill extraction, forensic anomaly detection, and Tally/QuickBooks reconciliation engine. Use when starting/stopping the FinSentry server, auditing bills, debugging extraction/reconciliation, or exporting accounting data.
---

# FinSentry Autonomous Financial Audit Skill

## When to Use
Use this skill whenever the user asks to:
- Start, stop, or restart the FinSentry cockpit server (`http://127.0.0.1:8000`).
- Upload, extract, audit, or reconcile vendor bills, receipts, or bank statements.
- Debug vision OCR extraction, Agent 1 forensic flags, Agent 2 Tally XML generation, or QuickBooks CSV exports.

## Environment & Prerequisites
- **Python Environment**: `.venv/bin/python3`
- **Configuration (`.env`)**:
  - `GROQ_API_KEY`: Required for image/scanned bill extraction (**Groq Vision `qwen/qwen3.8-27b` ONLY**, no fallbacks).
  - `LMSTUDIO_HOST` / `LMSTUDIO_MODEL`: Local text inference (`http://127.0.0.1:1234/v1`, `google/gemma-4-e4b`).
- **Directories**:
  - `./vault_drop`: Hot folder monitored by `watcher.py`.
  - `./vault_output`: SQLite database (`finsentry.db`), archived files (`processed_archive/`), and exports (`exports/`).

## Server Operations
- **Start Server (foreground)**:
  ```bash
  ./run.sh
  ```
- **Start Server (detached background)**:
  ```bash
  nohup ./run.sh > server.log 2>&1 &
  ```
- **Stop Server & Watcher**:
  ```bash
  ./stop.sh
  ```

## Two-Agent Pipeline & Rules
1. **Multimodal Extraction (`extractor.py`, `llm_client.py`)**:
   - Images (`.png`, `.jpg`, `.jpeg`) route exclusively to Groq Vision (`qwen/qwen3.8-27b`).
   - Digital PDFs and bank CSVs use `pdfplumber` / `pypdf`.
   - Extracts `vendor_name`, `invoice_number`, `invoice_date`, `due_date`, `subtotal`, `discount_amount`, `cgst_amount`, `sgst_amount`, `igst_amount`, `tax_amount`, `total_amount`.
2. **Agent 1 — Forensic Auditor (`agents.py`)**:
   - Checks zero/negative totals (`CRITICAL`), GST breakdown `CGST + SGST + IGST == tax_amount` (`HIGH`), final arithmetic `subtotal - discount + tax == total` (`HIGH`), `due_date < invoice_date` (`HIGH`), duplicate/near-duplicate invoices (`CRITICAL`), and BEC bank account changes (`CRITICAL`).
   - Approving a flag (`POST /api/flags/{flag_id}/status?status=RESOLVED`) promotes the invoice to `READY_FOR_TALLY`.
3. **Agent 2 — Tally Specialist (`agents.py`)**:
   - Converts `VERIFIED_CLEAN` / `READY_FOR_TALLY` invoices into balanced Tally ERP/Prime XML vouchers.
4. **Reconciliation & Exports (`reconciler.py`, `exporter.py`)**:
   - `POST /api/reconcile`: Runs Tier 1 deterministic matching, Tier 2 LLM entity resolution, smurfing detection, and phantom debit checks.
   - `GET /api/export/quickbooks`: Exports all `RECONCILED` bank transactions plus all `READY_FOR_TALLY` / `VERIFIED_CLEAN` / `RECONCILED` invoices.
   - `GET /api/export/tally`: Exports Tally XML vouchers.
