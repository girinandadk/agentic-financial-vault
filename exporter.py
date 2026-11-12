"""
FinSentry Exporter Engine
Generates:
1. QuickBooks-compatible CSV export
2. Tally-compatible Voucher XML export
3. Self-contained standalone HTML Executive Audit Report with CSS styling & metrics
"""
import csv
import io
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from config import EXPORTS_DIR, REPORTS_DIR
from db import get_db


def export_quickbooks_csv() -> Path:
    """Generates a QuickBooks Online compatible CSV export of reconciled transactions."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = EXPORTS_DIR / f"quickbooks_reconciled_{timestamp}.csv"

    with get_db() as conn:
        cur = conn.execute("""
            SELECT bt.txn_id, bt.txn_date, bt.raw_narration, bt.clean_merchant, bt.debit_amount, bt.credit_amount,
                   i.invoice_number, i.total_amount, i.invoice_date, v.name as vendor_name, v.standard_category
            FROM bank_transactions bt
            LEFT JOIN invoices i ON bt.matched_invoice_id = i.invoice_id
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE bt.status = 'RECONCILED'
            ORDER BY bt.txn_date ASC
        """)
        rows = cur.fetchall()

    with open(out_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Transaction Type", "Payee", "Category", "Description", 
            "Amount", "Invoice Number", "Match Status"
        ])
        for r in rows:
            amt = float(r["debit_amount"]) if r["debit_amount"] > 0 else -float(r["credit_amount"])
            writer.writerow([
                r["txn_date"],
                "Expense" if amt > 0 else "Deposit",
                r["vendor_name"] or r["clean_merchant"] or "Unknown Payee",
                r["standard_category"] or "Corporate Expenses",
                r["raw_narration"],
                f"{amt:.2f}",
                r["invoice_number"] or "N/A",
                "RECONCILED"
            ])

    return out_file


def export_tally_xml() -> Path:
    """Generates Tally-compatible XML voucher transactions."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = EXPORTS_DIR / f"tally_vouchers_{timestamp}.xml"

    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    tally_req = ET.SubElement(header, "TALLYREQUEST")
    tally_req.text = "Import Data"

    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    req_data = ET.SubElement(import_data, "REQUESTDATA")

    with get_db() as conn:
        cur = conn.execute("""
            SELECT bt.txn_id, bt.txn_date, bt.raw_narration, bt.debit_amount,
                   i.invoice_number, v.name as vendor_name, v.standard_category
            FROM bank_transactions bt
            LEFT JOIN invoices i ON bt.matched_invoice_id = i.invoice_id
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE bt.status = 'RECONCILED'
            ORDER BY bt.txn_date ASC
        """)
        rows = cur.fetchall()

    tally_msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})

    for r in rows:
        v_date = (r["txn_date"] or "").replace("-", "")
        amt = float(r["debit_amount"])
        v_name = r["vendor_name"] or "Sundry Creditors"
        
        voucher = ET.SubElement(tally_msg, "VOUCHER", {"VCHTYPE": "Payment", "ACTION": "Create"})
        ET.SubElement(voucher, "DATE").text = v_date
        ET.SubElement(voucher, "NARRATION").text = f"Inv #{r['invoice_number']} - {r['raw_narration']}"
        ET.SubElement(voucher, "VOUCHERTYPENAME").text = "Payment"

        # Debit entry (Vendor)
        all_ledger_dr = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(all_ledger_dr, "LEDGERNAME").text = v_name
        ET.SubElement(all_ledger_dr, "ISDEEMEDPOSITIVE").text = "Yes"
        ET.SubElement(all_ledger_dr, "AMOUNT").text = f"-{amt:.2f}"

        # Credit entry (Bank Account)
        all_ledger_cr = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(all_ledger_cr, "LEDGERNAME").text = "Corporate Operating Bank Account"
        ET.SubElement(all_ledger_cr, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(all_ledger_cr, "AMOUNT").text = f"{amt:.2f}"

    xml_str = minidom.parseString(ET.tostring(envelope, encoding="utf-8")).toprettyxml(indent="  ")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(xml_str)

    return out_file


def generate_executive_html_report() -> Path:
    """Generates a comprehensive, self-contained single-file HTML audit report."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = REPORTS_DIR / f"audit_executive_report_{timestamp}.html"

    with get_db() as conn:
        # Invoices stats
        tot_inv = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0.0) as s FROM invoices").fetchone()
        reconciled_inv = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(total_amount), 0.0) as s FROM invoices WHERE status = 'RECONCILED'").fetchone()

        # Bank stats
        tot_txns = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(debit_amount), 0.0) as s FROM bank_transactions WHERE debit_amount > 0").fetchone()
        reconciled_txns = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(debit_amount), 0.0) as s FROM bank_transactions WHERE status = 'RECONCILED'").fetchone()

        # Flags stats
        flags_cur = conn.execute("""
            SELECT flag_id, reference_type, reference_id, severity, issue_type, description, status, created_at
            FROM audit_flags
            ORDER BY CASE severity 
                WHEN 'CRITICAL' THEN 1 
                WHEN 'HIGH' THEN 2 
                WHEN 'MEDIUM' THEN 3 
                ELSE 4 END, flag_id DESC
        """)
        flags = flags_cur.fetchall()

        # Reconciled matches
        matches_cur = conn.execute("""
            SELECT bt.txn_id, bt.txn_date, bt.raw_narration, bt.debit_amount, bt.match_confidence,
                   i.invoice_number, i.invoice_date, i.total_amount, v.name as vendor_name
            FROM bank_transactions bt
            JOIN invoices i ON bt.matched_invoice_id = i.invoice_id
            JOIN vendors v ON i.vendor_id = v.vendor_id
            ORDER BY bt.txn_date DESC
        """)
        matches = matches_cur.fetchall()

        # Unmatched txns
        unmatched_cur = conn.execute("""
            SELECT txn_id, txn_date, raw_narration, debit_amount, status
            FROM bank_transactions
            WHERE status != 'RECONCILED' AND debit_amount > 0
            ORDER BY debit_amount DESC
        """)
        unmatched_txns = unmatched_cur.fetchall()

    # Severity counts
    critical_cnt = sum(1 for f in flags if f["severity"] == "CRITICAL" and f["status"] == "OPEN")
    high_cnt = sum(1 for f in flags if f["severity"] == "HIGH" and f["status"] == "OPEN")
    med_cnt = sum(1 for f in flags if f["severity"] == "MEDIUM" and f["status"] == "OPEN")

    match_pct = (reconciled_txns["c"] / tot_txns["c"] * 100) if tot_txns["c"] > 0 else 0.0

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FinSentry Executive Audit Report - {datetime.now().strftime('%B %d, %Y')}</title>
<style>
  :root {{
    --bg: #0f172a;
    --card: #1e293b;
    --border: #334155;
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --primary: #38bdf8;
    --critical: #ef4444;
    --high: #f97316;
    --medium: #eab308;
    --success: #22c55e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: var(--bg);
    color: var(--text);
    padding: 30px;
    line-height: 1.5;
  }}
  .header {{
    border-bottom: 2px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 30px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .logo {{ font-size: 24px; font-weight: 800; letter-spacing: -0.5px; color: var(--primary); }}
  .badge {{ display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; }}
  .badge-critical {{ background: rgba(239, 68, 68, 0.2); color: var(--critical); border: 1px solid var(--critical); }}
  .badge-high {{ background: rgba(249, 115, 22, 0.2); color: var(--high); border: 1px solid var(--high); }}
  .badge-medium {{ background: rgba(234, 179, 8, 0.2); color: var(--medium); border: 1px solid var(--medium); }}
  .badge-success {{ background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid var(--success); }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }}
  .metric-val {{ font-size: 28px; font-weight: 700; margin-top: 5px; }}
  .metric-lbl {{ font-size: 13px; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }}

  table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }}
  th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ background: rgba(0,0,0,0.2); color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
  tr:hover {{ background: rgba(255,255,255,0.02); }}

  .section-title {{ font-size: 18px; font-weight: 700; margin: 30px 0 15px; display: flex; align-items: center; gap: 10px; }}
  .footer {{ margin-top: 50px; font-size: 12px; color: var(--text-muted); text-align: center; border-top: 1px solid var(--border); padding-top: 20px; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="logo">FINSENTRY &bull; AUTONOMOUS AUDITING</div>
    <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">Executive Forensic Reconciliation & Internal Controls Dossier</div>
  </div>
  <div style="text-align: right;">
    <div style="font-size: 14px; font-weight: 600;">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
    <div style="font-size: 12px; color: var(--text-muted);">Engine: Gemma 4 (gemma4:e4b) Local Host</div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <div class="metric-lbl">Reconciliation Rate</div>
    <div class="metric-val" style="color: var(--primary);">{match_pct:.1f}%</div>
    <div style="font-size: 12px; color: var(--text-muted);">{reconciled_txns['c']} of {tot_txns['c']} Bank Debits Matched</div>
  </div>
  <div class="card">
    <div class="metric-lbl">Reconciled Outflow</div>
    <div class="metric-val" style="color: var(--success);">${float(reconciled_txns['s']):,.2f}</div>
    <div style="font-size: 12px; color: var(--text-muted);">Verified against Vendor Invoices</div>
  </div>
  <div class="card">
    <div class="metric-lbl">Critical Risk Alerts</div>
    <div class="metric-val" style="color: var(--critical);">{critical_cnt}</div>
    <div style="font-size: 12px; color: var(--text-muted);">BEC Bank Change & Duplicates</div>
  </div>
  <div class="card">
    <div class="metric-lbl">High Risk Warnings</div>
    <div class="metric-val" style="color: var(--high);">{high_cnt}</div>
    <div style="font-size: 12px; color: var(--text-muted);">Arithmetic & Smurfing Patterns</div>
  </div>
</div>

<div class="section-title">
  <span>1. Active Forensic Audit Flags</span>
  <span class="badge badge-critical">{len(flags)} TOTAL</span>
</div>
<div class="card" style="padding: 0; overflow-x: auto;">
  <table>
    <thead>
      <tr>
        <th>Severity</th>
        <th>Issue Type</th>
        <th>Reference</th>
        <th>Forensic Description</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
"""

    for f in flags:
        badge_cls = "badge-critical" if f["severity"] == "CRITICAL" else ("badge-high" if f["severity"] == "HIGH" else "badge-medium")
        html_content += f"""
      <tr>
        <td><span class="badge {badge_cls}">{f['severity']}</span></td>
        <td style="font-weight: 600;">{f['issue_type']}</td>
        <td>{f['reference_type']} #{f['reference_id']}</td>
        <td style="color: #e2e8f0;">{f['description']}</td>
        <td><span class="badge" style="background:#334155; color:#cbd5e1;">{f['status']}</span></td>
      </tr>
"""

    html_content += f"""
    </tbody>
  </table>
</div>

<div class="section-title">
  <span>2. Fully Reconciled Transactions (Two-Tier Matching)</span>
  <span class="badge badge-success">{len(matches)} RECONCILED</span>
</div>
<div class="card" style="padding: 0; overflow-x: auto;">
  <table>
    <thead>
      <tr>
        <th>Date</th>
        <th>Bank Narration</th>
        <th>Matched Vendor & Invoice</th>
        <th>Invoice Date</th>
        <th>Amount</th>
        <th>Confidence</th>
      </tr>
    </thead>
    <tbody>
"""

    for m in matches:
        html_content += f"""
      <tr>
        <td>{m['txn_date']}</td>
        <td>{m['raw_narration']}</td>
        <td><strong>{m['vendor_name']}</strong> <span style="color:var(--text-muted);">({m['invoice_number']})</span></td>
        <td>{m['invoice_date']}</td>
        <td style="font-weight:700;">${float(m['debit_amount']):,.2f}</td>
        <td><span class="badge badge-success">{int(float(m['match_confidence'])*100)}% Match</span></td>
      </tr>
"""

    html_content += f"""
    </tbody>
  </table>
</div>

<div class="section-title">
  <span>3. Unmatched Bank Debits / Phantom Transactions</span>
  <span class="badge badge-medium">{len(unmatched_txns)} PENDING</span>
</div>
<div class="card" style="padding: 0; overflow-x: auto;">
  <table>
    <thead>
      <tr>
        <th>Txn ID</th>
        <th>Date</th>
        <th>Bank Narration</th>
        <th>Debit Amount</th>
        <th>Audit Disposition</th>
      </tr>
    </thead>
    <tbody>
"""

    for u in unmatched_txns:
        html_content += f"""
      <tr>
        <td>#{u['txn_id']}</td>
        <td>{u['txn_date']}</td>
        <td>{u['raw_narration']}</td>
        <td style="color:var(--critical); font-weight:700;">${float(u['debit_amount']):,.2f}</td>
        <td><span class="badge badge-medium">Unreconciled / Investigation Required</span></td>
      </tr>
"""

    html_content += f"""
    </tbody>
  </table>
</div>

<div class="footer">
  FinSentry Local Autonomous Corporate Auditing Engine &bull; Zero external network telemetry &bull; SQLite WAL Mode
</div>

</body>
</html>
"""

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    return out_file
