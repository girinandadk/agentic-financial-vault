"""
FinSentry Local Host Server & Web Cockpit
FastAPI application serving the auditing dashboard, manual reconciliations,
flag management, document uploads, and one-click accounting exports.
"""
import os
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, UploadFile, File, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import VAULT_DROP_DIR, VAULT_OUTPUT_DIR, DB_PATH
from db import get_db, init_db
from reconciler import run_reconciliation
from agents import run_two_agent_pipeline, run_agent1_forensic_audit, run_agent2_tally_conversion, generate_tally_xml_from_voucher
from exporter import export_quickbooks_csv, export_tally_xml, generate_executive_html_report
from watcher import process_dropped_file

app = FastAPI(title="FinSentry Autonomous Corporate Auditing Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

class ManualMatchRequest(BaseModel):
    txn_id: int
    invoice_id: int


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Renders the main FinSentry Cockpit dashboard."""
    with get_db() as conn:
        # Metrics
        tot_txns = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(debit_amount), 0.0) as s FROM bank_transactions WHERE debit_amount > 0").fetchone()
        rec_txns = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(debit_amount), 0.0) as s FROM bank_transactions WHERE status = 'RECONCILED'").fetchone()
        unrec_txns = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(debit_amount), 0.0) as s FROM bank_transactions WHERE status != 'RECONCILED' AND debit_amount > 0").fetchone()
        
        tot_inv_cnt = conn.execute("SELECT COUNT(*) as c FROM invoices").fetchone()["c"]
        open_flags = conn.execute("SELECT COUNT(*) as c FROM audit_flags WHERE status = 'OPEN'").fetchone()["c"]
        critical_flags = conn.execute("SELECT COUNT(*) as c FROM audit_flags WHERE status = 'OPEN' AND severity = 'CRITICAL'").fetchone()["c"]

        # Lists
        top_flags = conn.execute("""
            SELECT f.flag_id, f.reference_type, f.reference_id, f.severity, f.issue_type, f.description, f.status,
                   COALESCE(i.raw_file_name, '') as raw_file_name,
                   COALESCE(i.invoice_number, '') as invoice_number
            FROM audit_flags f
            LEFT JOIN invoices i ON f.reference_type = 'INVOICE' AND f.reference_id = i.invoice_id
            WHERE f.status = 'OPEN'
            ORDER BY CASE f.severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, f.flag_id DESC
            LIMIT 8
        """).fetchall()

        all_flags = conn.execute("""
            SELECT f.flag_id, f.reference_type, f.reference_id, f.severity, f.issue_type, f.description, f.status, f.created_at,
                   COALESCE(i.raw_file_name, '') as raw_file_name,
                   COALESCE(i.invoice_number, '') as invoice_number
            FROM audit_flags f
            LEFT JOIN invoices i ON f.reference_type = 'INVOICE' AND f.reference_id = i.invoice_id
            ORDER BY CASE f.severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, f.flag_id DESC
        """).fetchall()

        reconciled_list = conn.execute("""
            SELECT bt.txn_id, bt.txn_date, bt.raw_narration, bt.debit_amount, bt.match_confidence,
                   i.invoice_number, i.invoice_date, i.total_amount, v.name as vendor_name
            FROM bank_transactions bt
            JOIN invoices i ON bt.matched_invoice_id = i.invoice_id
            JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE bt.status = 'RECONCILED'
            ORDER BY bt.txn_date DESC
        """).fetchall()

        unmatched_list = conn.execute("""
            SELECT txn_id, txn_date, raw_narration, debit_amount, status
            FROM bank_transactions
            WHERE status != 'RECONCILED' AND debit_amount > 0
            ORDER BY debit_amount DESC
        """).fetchall()

        all_invoices = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, i.total_amount, i.status, v.name as vendor_name
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            ORDER BY i.invoice_id DESC
        """).fetchall()

        open_invoices_dropdown = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.total_amount, v.name as vendor_name
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE i.status != 'RECONCILED'
            ORDER BY i.invoice_id DESC
        """).fetchall()

    reconciled_pct = (rec_txns["c"] / tot_txns["c"] * 100.0) if tot_txns["c"] > 0 else 0.0

    ctx = {
        "request": request,
        "vault_drop_dir": str(VAULT_DROP_DIR),
        "total_txns_cnt": tot_txns["c"],
        "reconciled_cnt": rec_txns["c"],
        "reconciled_pct": reconciled_pct,
        "reconciled_amount": float(rec_txns["s"]),
        "unmatched_cnt": unrec_txns["c"],
        "unmatched_amount": float(unrec_txns["s"]),
        "invoices_cnt": tot_inv_cnt,
        "open_flags_cnt": open_flags,
        "critical_flags_cnt": critical_flags,
        "top_flags": top_flags,
        "all_flags": all_flags,
        "reconciled_list": reconciled_list,
        "unmatched_list": unmatched_list,
        "all_invoices": all_invoices,
        "open_invoices_dropdown": open_invoices_dropdown,
    }
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), run_agents: bool = Query(True)):
    """Uploads bills directly into vault_drop, extracts structured data via LLM, and runs the 2-agent pipeline."""
    uploaded_files = []
    for file in files:
        target_path = VAULT_DROP_DIR / file.filename
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        uploaded_files.append(file.filename)
        # Trigger immediate processing
        process_dropped_file(target_path)
    
    if run_agents:
        run_two_agent_pipeline()
        run_reconciliation()

    return RedirectResponse(url="/?uploaded=" + str(len(uploaded_files)), status_code=303)


import threading

import json
import asyncio

@app.post("/api/upload-stream")
async def upload_stream(files: List[UploadFile] = File(...)):
    """Server-Sent Events endpoint that yields live real-time progress steps as the agents execute."""
    async def event_generator():
        yield f"data: {json.dumps({'step': 1, 'percent': 15, 'stage': 'INGESTION', 'msg': 'Saving & hashing uploaded bill...', 'detail': f'Received {len(files)} file(s)'})}\n\n"
        await asyncio.sleep(0.1)

        saved_paths = []
        for file in files:
            target_path = VAULT_DROP_DIR / file.filename
            with open(target_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_paths.append(target_path)
            
        yield f"data: {json.dumps({'step': 2, 'percent': 35, 'stage': 'EXTRACTION', 'msg': 'Multimodal LLM / OCR extracting bill fields...', 'detail': f'{files[0].filename}'})}\n\n"
        await asyncio.sleep(0.1)

        # Ingest and extract
        for path in saved_paths:
            process_dropped_file(path)

        yield f"data: {json.dumps({'step': 3, 'percent': 65, 'stage': 'AGENT_1_AUDIT', 'msg': 'Agent 1 checking arithmetic, BEC fraud, & duplicates...', 'detail': 'Verifying subtotal + tax = total'})}\n\n"
        await asyncio.sleep(0.1)

        # Run Two-Agent audit on the newest pending bill only
        run_two_agent_pipeline(only_new_pending=True)

        yield f"data: {json.dumps({'step': 4, 'percent': 85, 'stage': 'AGENT_2_TALLY', 'msg': 'Agent 2 formulating categorized Tally XML voucher...', 'detail': 'Ledger categorization & XML generation'})}\n\n"
        await asyncio.sleep(0.1)

        run_reconciliation()

        # Query the exact invoice that was uploaded in this session by matching uploaded filenames
        uploaded_names = [f.filename for f in files]
        with get_db() as conn:
            inv = None
            for fname in uploaded_names:
                inv = conn.execute("""
                    SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, i.total_amount, i.status, 
                           v.name as vendor_name, i.raw_file_name
                    FROM invoices i
                    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
                    WHERE i.raw_file_name = ?
                    ORDER BY i.invoice_id DESC
                    LIMIT 1
                """, (fname,)).fetchone()
                if inv:
                    break

            if not inv:
                inv = conn.execute("""
                    SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, i.total_amount, i.status, 
                           v.name as vendor_name, i.raw_file_name
                    FROM invoices i
                    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
                    ORDER BY i.invoice_id DESC
                    LIMIT 1
                """).fetchone()

        inv_data = dict(inv) if inv else None
        yield f"data: {json.dumps({'step': 5, 'percent': 100, 'stage': 'COMPLETE', 'msg': 'Audit & Tally compilation complete!', 'invoice': inv_data})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/upload-and-run")
async def upload_and_run(files: List[UploadFile] = File(...)):
    """AJAX endpoint: Saves files into vault_drop immediately, extracts data, audits with Agent 1, compiles with Agent 2, and returns structured result."""
    processed_details = []
    
    for file in files:
        target_path = VAULT_DROP_DIR / file.filename
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        process_dropped_file(target_path)

    # Run Two-Agent audit pipeline
    run_two_agent_pipeline(only_new_pending=False)
    run_reconciliation()

    # Fetch the latest processed invoices
    with get_db() as conn:
        latest_invs = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, i.total_amount, i.status, 
                   v.name as vendor_name, i.raw_file_name
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            ORDER BY i.invoice_id DESC
            LIMIT 5
        """).fetchall()

        latest_flags = conn.execute("""
            SELECT flag_id, reference_type, reference_id, severity, issue_type, description, status
            FROM audit_flags
            WHERE status = 'OPEN'
            ORDER BY flag_id DESC
            LIMIT 6
        """).fetchall()

    return JSONResponse({
        "status": "success",
        "message": f"Processed {len(files)} bill(s) through Agent 1 and Agent 2.",
        "invoices": [dict(r) for r in latest_invs],
        "flags": [dict(f) for f in latest_flags]
    })


@app.post("/api/reconcile")
def api_reconcile():
    """Manually triggers the reconciliation cycle."""
    run_reconciliation()
    return {"status": "success", "message": "Reconciliation cycle complete"}


@app.post("/api/flags/{flag_id}/status")
def update_flag_status(flag_id: int, status: str = Query(...)):
    """Updates an audit flag status (e.g. RESOLVED, DISMISSED, OPEN)."""
    with get_db() as conn:
        conn.execute("UPDATE audit_flags SET status = ? WHERE flag_id = ?", (status, flag_id))
    return {"status": "success", "flag_id": flag_id, "new_status": status}


@app.post("/api/manual-match")
def manual_match(req: ManualMatchRequest):
    """Allows auditors to manually pair an unmatched transaction with an open invoice."""
    with get_db() as conn:
        conn.execute("""
            UPDATE bank_transactions 
            SET matched_invoice_id = ?, match_confidence = 1.0, status = 'RECONCILED'
            WHERE txn_id = ?
        """, (req.invoice_id, req.txn_id))
        conn.execute("UPDATE invoices SET status = 'RECONCILED' WHERE invoice_id = ?", (req.invoice_id,))
    return {"status": "success", "txn_id": req.txn_id, "invoice_id": req.invoice_id}


@app.get("/api/export/quickbooks")
def export_qb():
    """Generates and downloads QuickBooks CSV."""
    csv_file = export_quickbooks_csv()
    return FileResponse(csv_file, media_type="text/csv", filename=csv_file.name)


@app.get("/api/export/tally")
def export_tally():
    """Generates and downloads Tally XML."""
    xml_file = export_tally_xml()
    return FileResponse(xml_file, media_type="application/xml", filename=xml_file.name)


@app.get("/api/export/report")
def export_report():
    """Generates and serves the standalone HTML executive audit report."""
    report_file = generate_executive_html_report()
    return FileResponse(report_file, media_type="text/html")



@app.post("/api/pipeline/run")
def api_run_pipeline():
    """Runs the complete Agent 1 (Forensic Audit) -> Agent 2 (Tally Voucher) pipeline."""
    run_two_agent_pipeline()
    run_reconciliation()
    return {"status": "success", "message": "Two-Agent pipeline and reconciliation complete"}


@app.get("/api/tally/preview/{invoice_id}")
def preview_tally_voucher(invoice_id: int):
    """Returns Agent 2's ready-to-use Tally XML for a verified invoice."""
    voucher = run_agent2_tally_conversion(invoice_id)
    if not voucher:
        raise HTTPException(status_code=400, detail="Invoice has not been verified clean by Agent 1 or failed conversion.")
    
    with get_db() as conn:
        inv = conn.execute("SELECT invoice_number FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
        inv_num = inv["invoice_number"] if inv else f"INV-{invoice_id}"
    
    xml_content = generate_tally_xml_from_voucher(voucher, inv_num)
    return HTMLResponse(content=f"<pre style='background:#0f172a; color:#38bdf8; padding:20px; border-radius:8px; font-family:monospace;'>{xml_content}</pre>")


from config import PROCESSED_ARCHIVE_DIR, VAULT_DROP_DIR

@app.get("/api/vault/view/{filename}")
def view_vault_file(filename: str):
    """Serves the raw document (PDF/PNG/JPG) directly from disk without database dependencies."""
    # Search in processed_archive, vault_drop, and sample_test_files
    candidate_dirs = [
        PROCESSED_ARCHIVE_DIR,
        VAULT_DROP_DIR,
        Path.home() / "Downloads" / "finsentry_test_bills",
        Path(__file__).parent / "sample_test_files"
    ]

    clean_name = Path(filename).name
    target_path = None

    for d in candidate_dirs:
        exact = d / clean_name
        if exact.exists():
            target_path = exact
            break
        # Also check with prefix timestamps (e.g., 179448..._filename)
        matches = list(d.glob(f"*{clean_name}*"))
        if matches:
            target_path = matches[0]
            break

    if not target_path or not target_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found on disk")

    suffix = target_path.suffix.lower()
    media_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".csv": "text/plain"
    }
    media_type = media_map.get(suffix, "application/octet-stream")
    return FileResponse(target_path, media_type=media_type)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
