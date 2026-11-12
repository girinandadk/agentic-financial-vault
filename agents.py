"""
FinSentry Two-Agent Autonomous Pipeline:
- Agent 1 (Forensic Auditor & Anomaly Detector):
  Inspects bills/invoices for arithmetic errors, duplicate submissions, account manipulation,
  BEC bank changes, and corruption of values.
  If any anomalies or corruption are detected, it halts promotion and raises flags to the user.
  If 100% verified and clean, it transitions status to 'VERIFIED_CLEAN' and passes the bill to Agent 2.

- Agent 2 (Tally Conversion & Accounting Ledger Specialist):
  Takes verified bills from Agent 1, determines standard corporate ledger categorization,
  tax breakdown (IGST/CGST/SGST/VAT), and compiles ready-to-import Tally Prime / ERP XML vouchers.
"""
import re
import json
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field

from db import get_db, hash_bank_account, mask_account_number
from llm_client import generate_structured_output
from reconciler import add_audit_flag

logger = logging.getLogger("finsentry.agents")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ==============================================================================
# SCHEMAS
# ==============================================================================

class Agent1AuditResult(BaseModel):
    is_valid: bool = Field(description="True if bill is clean, False if anomalies/errors/corruption detected")
    issues: List[str] = Field(default_factory=list, description="List of forensic issues discovered")
    arithmetic_valid: bool = Field(description="True if subtotal + tax equals total")
    account_manipulation_detected: bool = Field(description="True if suspicious bank account or BEC change found")
    severity: str = Field(default="NONE", description="NONE, LOW, MEDIUM, HIGH, CRITICAL")
    audit_notes: str = Field(description="Detailed forensic audit summary")


class Agent2TallyVoucher(BaseModel):
    voucher_type: str = Field(default="Purchase", description="Tally voucher type (Purchase, Payment, Journal)")
    voucher_date: str = Field(description="Date in YYYYMMDD or YYYY-MM-DD")
    vendor_ledger: str = Field(description="Party Ledger Name (Sundry Creditor)")
    expense_ledger: str = Field(description="Expense Account (e.g. Software Expense, Cloud Services, Legal Fees)")
    tax_ledger: Optional[str] = Field(default=None, description="Input Tax Ledger (e.g. Input IGST, Input GST, VAT)")
    amount_subtotal: float = Field(description="Pre-tax amount")
    amount_tax: float = Field(default=0.0, description="Tax amount")
    amount_total: float = Field(description="Final payable total")
    narration: str = Field(description="Structured standard Tally narration")
    cost_centre: Optional[str] = Field(default="Corporate HQ", description="Department/Cost centre")


# ==============================================================================
# AGENT 1: FORENSIC AUDITOR & VALUE INTEGRITY AGENT
# ==============================================================================

def run_agent1_forensic_audit(invoice_id: int) -> Agent1AuditResult:
    """
    Agent 1 audits an individual invoice in depth:
    1. Arithmetic balance check (subtotal + tax == total)
    2. Zero or negative / corrupted amounts
    3. Duplicate invoice numbers & near duplicates
    4. Vendor bank account changes / BEC manipulation
    5. LLM Deep Forensic Reasoning (gemma4:e4b / Gemini fallback)
    """
    with get_db() as conn:
        inv_row = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, 
                   i.total_amount, i.raw_file_name, i.status, i.vendor_id,
                   v.name as vendor_name, v.tax_id, v.bank_account_hash
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE i.invoice_id = ?
        """, (invoice_id,)).fetchone()

    if not inv_row:
        return Agent1AuditResult(
            is_valid=False,
            issues=["Invoice record not found"],
            arithmetic_valid=False,
            account_manipulation_detected=False,
            severity="CRITICAL",
            audit_notes="Invoice ID does not exist in database"
        )

    inv = dict(inv_row)
    issues: List[str] = []
    severity = "NONE"
    arithmetic_valid = True
    account_manipulation = False

    sub = float(inv["subtotal"])
    tax = float(inv["tax_amount"])
    tot = float(inv["total_amount"])

    # 1. Corrupted or Zero Values
    if tot <= 0.0:
        issues.append("Corrupted or zero value: Total amount is 0.0 or negative")
        severity = "CRITICAL"
        arithmetic_valid = False

    # 2. Arithmetic verification
    expected_tot = round(sub + tax, 2)
    if tot > 0 and abs(expected_tot - tot) > 0.05:
        issues.append(f"Arithmetic corruption: Subtotal (${sub:,.2f}) + Tax (${tax:,.2f}) = ${expected_tot:,.2f}, but Total is ${tot:,.2f}")
        severity = "HIGH" if severity != "CRITICAL" else "CRITICAL"
        arithmetic_valid = False

    # 3. Duplicate checks in DB (check against already reconciled or older records)
    with get_db() as conn:
        dup_rows = conn.execute("""
            SELECT invoice_id, invoice_number, total_amount, raw_file_name FROM invoices 
            WHERE invoice_id != ? AND UPPER(invoice_number) = UPPER(?)
        """, (inv["invoice_id"], inv["invoice_number"])).fetchall()

        for d in dup_rows:
            # Only flag as duplicate if it's truly a duplicate of a previously ingested distinct file
            if d["invoice_id"] < inv["invoice_id"]:
                issues.append(f"Duplicate invoice collision with previously ingested Invoice #{d['invoice_id']} ('{d['invoice_number']}')")
                severity = "CRITICAL"

    # 4. Check existing audit flags for this invoice
    with get_db() as conn:
        flags = conn.execute("""
            SELECT issue_type, description, severity FROM audit_flags
            WHERE reference_type = 'INVOICE' AND reference_id = ? AND status != 'DISMISSED'
        """, (inv["invoice_id"],)).fetchall()
        for fl in flags:
            issues.append(f"Existing flag [{fl['issue_type']}]: {fl['description']}")
            if fl["severity"] == "CRITICAL":
                severity = "CRITICAL"
                account_manipulation = True
            elif fl["severity"] == "HIGH" and severity != "CRITICAL":
                severity = "HIGH"

        # Check vendor bank account flag
        if inv["vendor_id"]:
            v_flags = conn.execute("""
                SELECT issue_type, description FROM audit_flags
                WHERE reference_type = 'VENDOR' AND reference_id = ? AND issue_type = 'VENDOR_BANK_CHANGE_BEC' AND status != 'DISMISSED'
            """, (inv["vendor_id"],)).fetchall()
            for vf in v_flags:
                issues.append(f"BEC Fraud Alert on Vendor: {vf['description']}")
                severity = "CRITICAL"
                account_manipulation = True

    # 5. LLM Deep Verification Check (Ollama gemma4:e4b / Gemini)
    prompt = (
        f"You are Agent 1 (Forensic Auditor & Internal Controls Guard).\n"
        f"Audit this corporate invoice for manipulation, fraud, or arithmetic anomalies:\n"
        f"- Vendor: {inv['vendor_name']}\n"
        f"- Invoice Number: {inv['invoice_number']}\n"
        f"- Date: {inv['invoice_date']}\n"
        f"- Subtotal: ${sub:.2f}\n"
        f"- Tax: ${tax:.2f}\n"
        f"- Total: ${tot:.2f}\n"
        f"- Identified Issues: {', '.join(issues) if issues else 'None so far'}\n\n"
        f"Verify the integrity of this bill. If subtotal + tax != total, or values are nonsensical, mark is_valid=false.\n"
        f"Return strictly valid JSON matching the schema."
    )

    llm_audit = generate_structured_output(prompt, Agent1AuditResult)
    if llm_audit:
        if not llm_audit.is_valid:
            for iss in llm_audit.issues:
                if iss not in issues:
                    issues.append(iss)
            if severity == "NONE":
                severity = llm_audit.severity if llm_audit.severity != "NONE" else "HIGH"
            arithmetic_valid = arithmetic_valid and llm_audit.arithmetic_valid
            account_manipulation = account_manipulation or llm_audit.account_manipulation_detected

    is_clean = len(issues) == 0 and tot > 0 and arithmetic_valid and not account_manipulation

    # Update invoice status in database
    new_status = "VERIFIED_CLEAN" if is_clean else "FLAGGED_ANOMALY"
    with get_db() as conn:
        conn.execute("UPDATE invoices SET status = ? WHERE invoice_id = ?", (new_status, inv["invoice_id"]))

        # If issues discovered, ensure audit flag is registered
        if not is_clean:
            for iss in issues:
                add_audit_flag(
                    conn,
                    ref_type="INVOICE",
                    ref_id=inv["invoice_id"],
                    severity=severity if severity != "NONE" else "HIGH",
                    issue_type="AGENT1_FORENSIC_CORRUPTION",
                    desc=f"Agent 1 Anomaly Flag: {iss}"
                )

    result = Agent1AuditResult(
        is_valid=is_clean,
        issues=issues,
        arithmetic_valid=arithmetic_valid,
        account_manipulation_detected=account_manipulation,
        severity=severity if not is_clean else "NONE",
        audit_notes=f"Agent 1 Audit: {'VERIFIED CLEAN' if is_clean else 'FLAGGED FOR ANOMALIES'} ({len(issues)} issues detected)."
    )
    logger.info(f"[AGENT 1] Invoice #{inv['invoice_id']} ({inv['invoice_number']}) -> {new_status} (Severity: {result.severity})")
    return result


# ==============================================================================
# AGENT 2: TALLY CONVERSION & ACCOUNTING LEDGER SPECIALIST
# ==============================================================================

def run_agent2_tally_conversion(invoice_id: int) -> Optional[Agent2TallyVoucher]:
    """
    Agent 2 takes a verified bill from Agent 1 and compiles it into a categorized ready-to-use Tally format.
    If the bill is NOT verified by Agent 1 (still flagged or corrupt), Agent 2 refuses to process it.
    """
    with get_db() as conn:
        inv_row = conn.execute("""
            SELECT i.invoice_id, i.invoice_number, i.invoice_date, i.subtotal, i.tax_amount, 
                   i.total_amount, i.raw_file_name, i.status, i.vendor_id,
                   v.name as vendor_name, v.tax_id, v.standard_category
            FROM invoices i
            LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
            WHERE i.invoice_id = ?
        """, (invoice_id,)).fetchone()

    if not inv_row:
        logger.error(f"[AGENT 2] Invoice #{invoice_id} not found.")
        return None

    inv = dict(inv_row)

    # STRICT GATE: Only process if Agent 1 verified it clean or reconciled
    if inv["status"] not in ["VERIFIED_CLEAN", "RECONCILED"]:
        logger.warning(f"[AGENT 2 REJECTION] Invoice #{invoice_id} status is '{inv['status']}'. Agent 2 only processes verified clean bills.")
        return None

    vendor_name = inv["vendor_name"] or "Sundry Creditor"
    category = inv["standard_category"] or "Corporate Expenses"
    clean_date = (inv["invoice_date"] or datetime.now().strftime("%Y-%m-%d")).replace("-", "")

    # LLM Categorization & Tally Voucher Formulation (gemma4:e4b / Gemini)
    prompt = (
        f"You are Agent 2 (Tally Accounting Ledger & Format Specialist).\n"
        f"Formulate a production-grade Tally ERP/Prime voucher for this verified corporate bill:\n"
        f"- Vendor/Supplier: {vendor_name}\n"
        f"- Invoice No: {inv['invoice_number']}\n"
        f"- Date: {inv['invoice_date']}\n"
        f"- Subtotal (Pre-tax): ${float(inv['subtotal']):.2f}\n"
        f"- Tax Amount: ${float(inv['tax_amount']):.2f}\n"
        f"- Total Amount: ${float(inv['total_amount']):.2f}\n"
        f"- Business Nature: {category}\n\n"
        f"Determine the correct standard Tally Purchase/Expense ledger name (e.g. 'Software & Cloud Services', 'Office Equipment', 'Legal & Professional Charges', 'Logistics Freight Ledger') "
        f"and the tax ledger name (e.g. 'Input GST / VAT Account'). Formulate a structured formal corporate narration.\n"
        f"Return strictly valid JSON matching the schema."
    )

    tally_voucher = generate_structured_output(prompt, Agent2TallyVoucher)

    # Algorithmic fallback if LLM is offline
    if not tally_voucher:
        expense_ledger = category
        v_low = vendor_name.lower()
        if any(x in v_low for x in ["cloud", "tech", "aws", "software", "apex"]):
            expense_ledger = "Software & IT Infrastructure Services"
        elif any(x in v_low for x in ["legal", "lex", "counsel", "advisory"]):
            expense_ledger = "Legal & Professional Consultation"
        elif any(x in v_low for x in ["logistics", "freight", "transport", "nexus"]):
            expense_ledger = "Freight & Inward Logistics Charges"
        elif any(x in v_low for x in ["industrial", "hardware", "acme"]):
            expense_ledger = "Plant & Machinery Maintenance / Hardware"

        tax_ledger = "Input Tax / VAT Account" if float(inv["tax_amount"]) > 0 else None
        narration = f"Being amount booked against {vendor_name} Inv #{inv['invoice_number']} dated {inv['invoice_date']}"

        tally_voucher = Agent2TallyVoucher(
            voucher_type="Purchase",
            voucher_date=clean_date,
            vendor_ledger=vendor_name,
            expense_ledger=expense_ledger,
            tax_ledger=tax_ledger,
            amount_subtotal=float(inv["subtotal"]),
            amount_tax=float(inv["tax_amount"]),
            amount_total=float(inv["total_amount"]),
            narration=narration,
            cost_centre="Corporate HQ"
        )

    # Update invoice in DB with status READY_FOR_TALLY
    with get_db() as conn:
        conn.execute("UPDATE invoices SET status = 'READY_FOR_TALLY' WHERE invoice_id = ?", (inv["invoice_id"],))

    logger.info(f"[AGENT 2 SUCCESS] Invoice #{invoice_id} compiled into ready-to-use Tally format ({tally_voucher.expense_ledger} -> {tally_voucher.vendor_ledger}).")
    return tally_voucher


def generate_tally_xml_from_voucher(v: Agent2TallyVoucher, invoice_num: str) -> str:
    """Formats an Agent2TallyVoucher model into standard Tally XML format."""
    xml = f"""<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <VOUCHER VCHTYPE="{v.voucher_type}" ACTION="Create">
    <DATE>{v.voucher_date.replace('-', '')}</DATE>
    <NARRATION>{v.narration}</NARRATION>
    <VOUCHERTYPENAME>{v.voucher_type}</VOUCHERTYPENAME>
    <REFERENCE>{invoice_num}</REFERENCE>
    <!-- Expense Debit -->
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{v.expense_ledger}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
      <AMOUNT>-{v.amount_subtotal:.2f}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
"""
    if v.amount_tax > 0 and v.tax_ledger:
        xml += f"""    <!-- Tax Debit -->
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{v.tax_ledger}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
      <AMOUNT>-{v.amount_tax:.2f}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
"""
    xml += f"""    <!-- Vendor Credit -->
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{v.vendor_ledger}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>{v.amount_total:.2f}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
  </VOUCHER>
</TALLYMESSAGE>"""
    return xml


def run_two_agent_pipeline(only_new_pending: bool = False):
    """
    Executes the complete 2-agent workflow across pending invoices:
    - Step 1: Agent 1 audits each pending bill.
    - Step 2: For bills that pass Agent 1 verification, Agent 2 converts them into ready-to-use Tally format.
    """
    logger.info("Executing FinSentry Two-Agent Pipeline...")
    with get_db() as conn:
        if only_new_pending:
            # Audit newly ingested bills immediately
            pending_rows = conn.execute("SELECT invoice_id, invoice_number FROM invoices WHERE status = 'PENDING' ORDER BY invoice_id DESC LIMIT 5").fetchall()
        else:
            pending_rows = conn.execute("SELECT invoice_id, invoice_number FROM invoices WHERE status IN ('PENDING', 'FLAGGED_ANOMALY')").fetchall()

    logger.info(f"Agent 1 reviewing {len(pending_rows)} invoices...")
    verified_count = 0
    flagged_count = 0

    for r in pending_rows:
        inv_id = r["invoice_id"]
        audit_res = run_agent1_forensic_audit(inv_id)
        if audit_res.is_valid:
            verified_count += 1
            # Step 2: Pass to Agent 2
            run_agent2_tally_conversion(inv_id)
        else:
            flagged_count += 1

    logger.info(f"Two-Agent Pipeline completed: {verified_count} verified & converted to Tally format, {flagged_count} halted by Agent 1 flags.")
