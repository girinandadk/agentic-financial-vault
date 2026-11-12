"""
FinSentry Local Host Server & Web Cockpit
FastAPI application serving the auditing dashboard, manual reconciliations,
flag management, document uploads, and one-click accounting exports.
"""
import os
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
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
            SELECT flag_id, reference_type, reference_id, severity, issue_type, description, status
            FROM audit_flags
            WHERE status = 'OPEN'
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, flag_id DESC
            LIMIT 6
        """).fetchall()

        all_flags = conn.execute("""
            SELECT flag_id, reference_type, reference_id, severity, issue_type, description, status, created_at
            FROM audit_flags
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END, flag_id DESC
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


@app.post("/api/upload-and-run")
async def upload_and_run(files: List[UploadFile] = File(...)):
    """AJAX endpoint: Uploads bills, executes Agent 1 & Agent 2 dynamically, and returns JSON audit results."""
    results = []
    for file in files:
        target_path = VAULT_DROP_DIR / file.filename
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        process_dropped_file(target_path)

    # Run Two-Agent pipeline
    run_two_agent_pipeline()
    run_reconciliation()

    # Query latest state
    with get_db() as conn:
        invoices = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, i.total_amount, i.status, v.name as vendor_name
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            ORDER BY i.invoice_id DESC
            LIMIT 10
        """).fetchall()

        flags = conn.execute("""
            SELECT flag_id, reference_type, reference_id, severity, issue_type, description, status
            FROM audit_flags
            WHERE status = 'OPEN'
            ORDER BY flag_id DESC
            LIMIT 10
        """).fetchall()

    return JSONResponse({
        "status": "success",
        "message": f"Processed {len(files)} bills through Agent 1 (Forensic Auditor) and Agent 2 (Tally Voucher Engine).",
        "invoices": [dict(r) for r in invoices],
        "flags": [dict(f) for f in flags]
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
