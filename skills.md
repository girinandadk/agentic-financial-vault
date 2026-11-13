# FinSentry Agent Skills & Capabilities

## 1. Multimodal Bill & Document Extraction (`extractor.py`, `llm_client.py`)
- **Image / Scanned Bill OCR**: Exclusively uses **Groq Vision (`qwen/qwen3.8-27b`)** via `GROQ_API_KEY` in `.env` (no fallback) to extract structured Pydantic fields:
  - `vendor_name`, `tax_id`, `invoice_number`, `invoice_date`, `due_date`
  - `subtotal`, `discount_amount`, `cgst_amount`, `sgst_amount`, `igst_amount`, `tax_amount`, `total_amount`
  - `category`, `bank_account_last4`
- **Digital PDF & Bank CSV Parsing**: Uses `pdfplumber` and `pypdf` for digital PDFs and CSV bank statements.

## 2. Agent 1: Forensic Value & Anomaly Auditor (`agents.py`)
Audits every ingested invoice before ledger promotion:
- **Zero / Corrupted Value Check**: Flags `total_amount <= 0.0` (`CRITICAL`).
- **GST Breakdown Verification**: Verifies `CGST + SGST + IGST == tax_amount` (`HIGH`).
- **Final Amount Arithmetic Check**: Verifies `subtotal - discount_amount + tax_amount == total_amount` (`HIGH`).
- **Date Logic Check**: Flags `due_date < invoice_date` (`HIGH`).
- **Duplicate & Near-Duplicate Detection**: Checks exact and fuzzy invoice number collisions (`CRITICAL`).
- **BEC Fraud Detection**: Compares vendor `bank_account_hash` against historical records (`CRITICAL`).
- **Auditor Override**: Approving a flag (`RESOLVED`) in the cockpit promotes the invoice to `READY_FOR_TALLY`.

## 3. Agent 2: Tally Accounting & Voucher Specialist (`agents.py`)
- Categorizes verified clean bills (`VERIFIED_CLEAN` / `READY_FOR_TALLY`) into Tally ERP / Prime XML vouchers with expense ledgers, input tax ledgers, and balanced debit/credit entries.

## 4. Two-Tier Hybrid Reconciliation (`reconciler.py`)
- **Tier 1 (Deterministic)**: Matches open invoices to bank debits using `±0.05` amount tolerance, `-5` to `+30` day window, and `rapidfuzz >= 88` vendor similarity.
- **Tier 2 (LLM Entity Resolution)**: Uses local **LM Studio (`google/gemma-4-e4b`)** (with Ollama/Gemini fallback for text) to resolve ambiguous bank narrations.
- **Smurfing & Phantom Debit Checks**: Detects threshold-avoidance splitting (`< $10,000` in 14 days) and unmatched bank debits `>= $500`.

## 5. Accounting Exports (`exporter.py`)
- **QuickBooks CSV**: Exports all `RECONCILED` bank transactions plus all `READY_FOR_TALLY` / `VERIFIED_CLEAN` / `RECONCILED` invoices.
- **Tally XML**: Generates import-ready Tally XML vouchers.
- **Executive HTML Report**: Generates a standalone single-file audit report.
