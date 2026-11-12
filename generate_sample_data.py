"""
FinSentry Realistic Sample Data Generator
Generates:
1. Valid vendor invoices (PDF & PNG)
2. Injected Forensic Anomalies:
   - Arithmetic Error: Subtotal $4,000 + Tax $400 != Total $5,200 (Invoice #INV-2024-ERR)
   - Near-duplicate Invoice: #INV-104 vs #INV-104-A
   - Vendor Bank Change (BEC Fraud): Acme Corp invoice with changed bank account ending in 9999
3. Bank Statement CSV with:
   - Matching debits for legitimate invoices
   - Gateway abbreviations & messy narrations for fuzzy entity resolution
   - High-value Phantom Debit (>= $500 without invoice)
   - Rapid transactions testing smurfing detection
"""
import os
import csv
from datetime import datetime, timedelta
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from PIL import Image, ImageDraw, ImageFont

from config import VAULT_DROP_DIR, VAULT_OUTPUT_DIR
from db import init_db, get_db, hash_bank_account

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_test_files"
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)


def create_pdf_invoice(filename: str, vendor: str, inv_num: str, inv_date: str, subtotal: float, tax: float, total: float, bank_acc: str, tax_id: str = "US-EIN-9928192"):
    path = SAMPLE_DIR / filename
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.setFillColor(colors.HexColor("#1e293b"))
    c.drawString(50, height - 60, vendor.upper())

    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#64748b"))
    c.drawString(50, height - 76, f"Tax ID / EIN: {tax_id}")
    c.drawString(50, height - 90, f"Remittance Bank A/C: {bank_acc}")

    # Invoice Info Box
    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(colors.HexColor("#0f172a"))
    c.drawString(380, height - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(380, height - 76, f"Invoice #: {inv_num}")
    c.drawString(380, height - 90, f"Invoice Date: {inv_date}")
    c.drawString(380, height - 104, f"Due Date: {inv_date}")

    # Line Separator
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.line(50, height - 120, width - 50, height - 120)

    # Line Item Table Header
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, height - 145, "Description")
    c.drawRightString(width - 50, height - 145, "Amount (USD)")
    c.line(50, height - 155, width - 50, height - 155)

    # Items
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 180, f"Enterprise Corporate Services for {vendor}")
    c.drawRightString(width - 50, height - 180, f"${subtotal:,.2f}")

    # Totals
    c.line(350, height - 240, width - 50, height - 240)
    c.drawString(360, height - 260, "Subtotal:")
    c.drawRightString(width - 50, height - 260, f"${subtotal:,.2f}")

    c.drawString(360, height - 280, "Tax (VAT/GST):")
    c.drawRightString(width - 50, height - 280, f"${tax:,.2f}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(360, height - 310, "Total Due:")
    c.drawRightString(width - 50, height - 310, f"${total:,.2f}")

    c.save()
    return path


def create_image_receipt(filename: str, vendor: str, inv_num: str, inv_date: str, total: float):
    path = SAMPLE_DIR / filename
    img = Image.new("RGB", (600, 450), color="#ffffff")
    draw = ImageDraw.Draw(img)

    # Clean text layout
    draw.rectangle([20, 20, 580, 430], outline="#0284c7", width=3)
    draw.text((40, 40), f"RECEIPT: {vendor.upper()}", fill="#0f172a")
    draw.text((40, 75), f"Invoice #: {inv_num}", fill="#334155")
    draw.text((40, 105), f"Date: {inv_date}", fill="#334155")
    draw.text((40, 135), "Category: IT Infrastructure & Hosting", fill="#334155")
    draw.line([(40, 170), (560, 170)], fill="#94a3b8", width=2)

    draw.text((40, 200), "Subtotal:", fill="#0f172a")
    draw.text((450, 200), f"${total*0.9:,.2f}", fill="#0f172a")

    draw.text((40, 230), "Tax (10%):", fill="#0f172a")
    draw.text((450, 230), f"${total*0.1:,.2f}", fill="#0f172a")

    draw.line([(40, 270), (560, 270)], fill="#0284c7", width=2)
    draw.text((40, 290), "TOTAL PAID:", fill="#0284c7")
    draw.text((450, 290), f"${total:,.2f}", fill="#0284c7")

    draw.text((40, 360), "Bank A/C: ****4421 | Auth ID: AUTH-88271", fill="#64748b")

    img.save(path)
    return path


def generate_all_samples():
    print("Generating FinSentry realistic test files...")

    # Initialize DB to seed historical vendor bank account
    init_db()
    with get_db() as conn:
        # Pre-seed Acme Corp historical legitimate bank account ending in 1111
        acme_bank_hash = hash_bank_account("1111")
        conn.execute("""
            INSERT OR REPLACE INTO vendors (vendor_id, name, tax_id, bank_account_hash, standard_category)
            VALUES (1, 'Acme Industrial Corp', 'US-EIN-1234567', ?, 'Manufacturing & Hardware')
        """, (acme_bank_hash,))

    # 1. Normal Invoices
    create_pdf_invoice("inv_01_acme.pdf", "Acme Industrial Corp", "INV-101", "2026-11-01", 1200.00, 120.00, 1320.00, "1111")
    create_pdf_invoice("inv_02_apex_cloud.pdf", "Apex Cloud Technologies", "INV-102", "2026-11-03", 2500.00, 250.00, 2750.00, "2222")
    create_pdf_invoice("inv_03_lex_legal.pdf", "Lex & Partners Legal", "INV-103", "2026-11-05", 5000.00, 0.00, 5000.00, "3333")

    # 2. Near-Duplicate Invoice Anomaly (INV-104 vs INV-104-A)
    create_pdf_invoice("inv_04_nexus_dupe1.pdf", "Nexus Logistics LLC", "INV-104", "2026-11-06", 1800.00, 180.00, 1980.00, "4444")
    create_pdf_invoice("inv_04_nexus_dupe2.pdf", "Nexus Logistics LLC", "INV-104-A", "2026-11-07", 1800.00, 180.00, 1980.00, "4444")

    # 3. Arithmetic Mismatch Anomaly: Subtotal 4000 + Tax 400 != Total 5200
    create_pdf_invoice("inv_05_arithmetic_err.pdf", "Vanguard Security Solutions", "INV-2024-ERR", "2026-11-08", 4000.00, 400.00, 5200.00, "5555")

    # 4. Vendor Bank Change BEC Fraud: Acme Corp invoice with changed bank ending in 9999
    create_pdf_invoice("inv_06_acme_bec_fraud.pdf", "Acme Industrial Corp", "INV-106-BEC", "2026-11-09", 3500.00, 350.00, 3850.00, "9999")

    # 5. Image Receipt
    create_image_receipt("inv_07_receipt.png", "Quantum Fast Hosting", "RCPT-771", "2026-11-10", 650.00)

    # 6. Smurfing Invoices (2 invoices to same vendor within 14 days, each < 10k, sum >= 15k)
    create_pdf_invoice("inv_08_smurf_a.pdf", "Helios Advisory Services", "INV-SMURF-1", "2026-11-02", 8000.00, 500.00, 8500.00, "7777")
    create_pdf_invoice("inv_09_smurf_b.pdf", "Helios Advisory Services", "INV-SMURF-2", "2026-11-05", 7500.00, 500.00, 8000.00, "7777")

    # 7. Bank Statement CSV
    csv_path = SAMPLE_DIR / "bank_statement_nov2026.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Narration", "Withdrawal Amount", "Deposit Amount", "Reference No"])
        writer.writerow(["2026-11-02", "WIRE/ACH-ACME INDUSTRIAL CORP PMT", "1320.00", "0.00", "REF-9901"])
        writer.writerow(["2026-11-04", "BILLDESK*APEX CLOUD TECH SERV", "2750.00", "0.00", "REF-9902"])
        writer.writerow(["2026-11-06", "PAYMENT TO LEX & PARTNERS ADV", "5000.00", "0.00", "REF-9903"])
        writer.writerow(["2026-11-07", "CHECK #8821 NEXUS LOGISTICS", "1980.00", "0.00", "REF-9904"])
        # Fuzzy narration for Quantum Hosting
        writer.writerow(["2026-11-11", "POS STRIPE*QUANTUM FAST HOST", "650.00", "0.00", "REF-9905"])
        # Unmatched Phantom Debit (>= $500)
        writer.writerow(["2026-11-10", "UNKNOWN DEBIT TXN TO OFFSHORE HOLDINGS", "1450.00", "0.00", "REF-PHANTOM"])

    print(f"Sample test suite created successfully in: {SAMPLE_DIR}")
    print("Files created:")
    for p in SAMPLE_DIR.iterdir():
        print(f" - {p.name}")

if __name__ == "__main__":
    generate_all_samples()
