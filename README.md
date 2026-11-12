# FinSentry 🛡️

**Local, Offline-First Autonomous Corporate Financial Auditing & Reconciliation Engine**
Powered by **Ollama (`gemma4:e4b`)** with zero external network telemetry.

---

## 🏗️ Architecture & Core Components

1. **Local Server & Inference**:
   - Primary LLM: Local Ollama (`http://localhost:11434`) model `gemma4:e4b` (`num_ctx: 4096`, `think: false`).
   - Resilient Fallback: Built-in local OCR and heuristic entity resolution when Ollama is offline or uninstalled.
2. **Directory Structure**:
   - `./vault_drop`: Monitored hot folder for incoming PDF invoices, receipt images, and CSV bank statements (configurable via `FINSENTRY_VAULT_DROP` for VirtualBox `/mnt/vault_drop` mounts).
   - `./vault_output`: SQLite WAL database (`finsentry.db`), SHA-256 deduplicated archive (`processed_archive/`), accounting exports (`exports/`), and executive reports (`reports/`).
3. **Web Cockpit (FastAPI + Embedded Dark Mode UI)**:
   - Hosted at `http://127.0.0.1:8000`.
   - Real-time audit dashboard, forensic flags inspector, two-tier reconciled ledger, manual match interface, and one-click accounting exports.
4. **Multimodal Extraction (`extractor.py`)**:
   - `pdfplumber` for digital PDFs and CSV bank statements.
   - `gemma4:e4b` multimodal vision for scanned PDFs and image receipts.
   - Account masking and SHA-256 bank hashing for strict data privacy.
5. **Resilient Folder Watcher (`watcher.py`)**:
   - `watchdog.observers.polling.PollingObserver` with multi-step file-size stability checks (prevents reading mid-write).
   - Deduplication via file SHA-256 hash before ingestion.
6. **Two-Tier Hybrid Reconciliation & Forensics (`reconciler.py`)**:
   - **Tier 1 (Deterministic)**: `±0.05` amount tolerance, `-5` to `+30` day window, `rapidfuzz` similarity (`>= 88`).
   - **Tier 2 (Gemma 4 Entity Resolution)**: Escalates ambiguous narrations (gateways, abbreviations) to structured Pydantic JSON schema matching.
   - **Forensic Controls**:
     - Arithmetic mismatch (`subtotal + tax != total`).
     - Duplicate & near-duplicate invoice numbers (`INV-104` vs `INV-104-A`).
     - Vendor bank account change (Business Email Compromise - BEC fraud detection).
     - Smurfing / Approval threshold avoidance (multiple invoices within 14 days under approval threshold but exceeding it in aggregate).
     - Unmatched phantom bank debits (`>= $500`).
7. **Accounting Exporter (`exporter.py`)**:
   - QuickBooks Online compatible CSV.
   - Tally ERP / Prime compatible XML vouchers.
   - Standalone, self-contained single-file HTML Executive Audit Report.

---

## 🚀 Quickstart & Execution

### 1. Requirements & Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Generate Sample Test Data
Creates realistic mock vendor invoices, bank statements, and injected forensic anomalies (BEC fraud, arithmetic error, near-duplicate, smurfing, and phantom debits):
```bash
python generate_sample_data.py
```

### 3. Run FinSentry
Starts both the background resilient folder watcher and the FastAPI server on port 8000:
```bash
./run.sh
```

Then open your browser to **[http://127.0.0.1:8000](http://127.0.0.1:8000)**.

---

## 🤖 Two-Agent Autonomous Pipeline

FinSentry implements an autonomous two-tier agentic architecture:

### **Agent 1: Forensic Value & Anomaly Auditor**
- **Role:** Deep forensic verification of incoming bills before any ledger booking.
- **Audits Performed:**
  - Arithmetic integrity (`subtotal + tax == total`).
  - Corrupted or zero/negative bill values.
  - Duplicate & near-duplicate invoice submissions (`INV-104` vs `INV-104-A`).
  - Business Email Compromise (BEC) vendor bank account changes.
  - Smurfing / approval threshold avoidance patterns.
- **Outcome:** If any anomalies or corruption are detected, Agent 1 halts promotion, flags the issue with severity (`CRITICAL`, `HIGH`, `MEDIUM`), and surfaces it in the web cockpit. Only 100% verified bills transition to `VERIFIED_CLEAN` and are handed over to Agent 2.

### **Agent 2: Tally Accounting & Voucher Specialist**
- **Role:** Takes verified clean bills from Agent 1 and compiles them into ready-to-use Tally ERP / Prime format.
- **Operations:**
  - Determines standard expense ledger categorizations (e.g. `Software & IT Infrastructure Services`, `Legal & Professional Charges`, `Freight & Inward Logistics Charges`).
  - Identifies tax ledger entries (`Input Tax / VAT Account`, `Input IGST`).
  - Formulates structured accounting narrations and references.
  - Outputs production-ready Tally XML vouchers with debit and credit balance rules.

---

## ⚡ Local Inference & Gemini API Fallback

FinSentry is optimized for local machines running **Ollama (`gemma4:e4b`)** with automatic fallback to **Gemini API**:
- **Local Ollama Optimization (`llm_client.py`)**:
  - `num_ctx: 4096`
  - `"think": false` for low latency
  - Deterministic `temperature: 0.1`
  - `num_predict: 1024`
  - Fast connection timeout (8.0s) for responsive local execution.
- **Automatic Gemini API Fallback**:
  - Place your `GEMINI_API_KEY` in `.env`.
  - When local Ollama is offline, unreachable, or in VM setup, FinSentry automatically switches to Gemini (`gemini-2.5-flash`) via the official `google-genai` SDK.
