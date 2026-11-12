import os
from PIL import Image, ImageDraw, ImageFont

def generate_bill_image(output_path="test_bill_sample.png"):
    width = 900
    height = 1100
    
    # White background with subtle paper texture
    img = Image.new("RGB", (width, height), color="#fcfcfd")
    draw = ImageDraw.Draw(img)

    # Outer border & header bar
    draw.rectangle([(20, 20), (width - 20, height - 20)], outline="#cbd5e1", width=2)
    draw.rectangle([(20, 20), (width - 20, 140)], fill="#0f172a")

    # Header title
    draw.text((50, 45), "HYPERION LOGIX TECHNOLOGIES INC.", fill="#38bdf8")
    draw.text((50, 85), "Enterprise AI Cloud Compute & Data Infrastructure Services", fill="#94a3b8")
    draw.text((50, 105), "742 Innovation Way, Suite 400, San Francisco, CA | GST/Tax ID: US-EIN-94810294", fill="#64748b")

    # Invoice Metadata Box (Right header)
    draw.text((width - 320, 45), "TAX INVOICE", fill="#ffffff")
    draw.text((width - 320, 75), "Invoice #: INV-2026-HL-882", fill="#e2e8f0")
    draw.text((width - 320, 95), "Date: 2026-11-12", fill="#e2e8f0")
    draw.text((width - 320, 115), "Due Date: 2026-12-12", fill="#38bdf8")

    # Billed To Section
    draw.rectangle([(50, 170), (450, 270)], fill="#f1f5f9", outline="#e2e8f0", width=1)
    draw.text((65, 185), "BILLED TO (CUSTOMER):", fill="#475569")
    draw.text((65, 205), "Acme Corporation Global HQ", fill="#0f172a")
    draw.text((65, 225), "Attn: Financial Controller / Accounts Payable", fill="#334155")
    draw.text((65, 245), "Client Account Ref: CL-88219-CORP", fill="#64748b")

    # Remittance Bank Details Box (Left)
    draw.rectangle([(width - 400, 170), (width - 50, 270)], fill="#f8fafc", outline="#cbd5e1", width=1)
    draw.text((width - 385, 185), "REMITTANCE INSTRUCTIONS:", fill="#0f172a")
    draw.text((width - 385, 205), "Bank: JPMorgan Chase NA", fill="#334155")
    draw.text((width - 385, 225), "Bank Account: ****7731", fill="#0284c7")
    draw.text((width - 385, 245), "Routing / SWIFT: CHASEUS33XXX", fill="#64748b")

    # Line Item Table Header
    table_top = 310
    draw.rectangle([(50, table_top), (width - 50, table_top + 40)], fill="#1e293b")
    draw.text((70, table_top + 12), "#", fill="#94a3b8")
    draw.text((110, table_top + 12), "Description of Goods & Cloud Services", fill="#f8fafc")
    draw.text((520, table_top + 12), "Qty / Hrs", fill="#f8fafc")
    draw.text((630, table_top + 12), "Unit Rate", fill="#f8fafc")
    draw.text((750, table_top + 12), "Total (USD)", fill="#f8fafc")

    # Items
    items = [
        ("1", "Cluster H100 GPU Dedicated Inference Node (30 Days)", "1 Node", "$2,800.00", "$2,800.00"),
        ("2", "Ultra-Low Latency NVMe Storage Allocation (10TB SSD)", "10 TB", "$70.00", "$700.00"),
        ("3", "Enterprise Dedicated SLA & Network Peering Package", "1 Mo", "$500.00", "$500.00")
    ]

    curr_y = table_top + 40
    for idx, (num, desc, qty, rate, amt) in enumerate(items):
        bg_col = "#ffffff" if idx % 2 == 0 else "#f8fafc"
        draw.rectangle([(50, curr_y), (width - 50, curr_y + 45)], fill=bg_col, outline="#e2e8f0", width=1)
        draw.text((70, curr_y + 14), num, fill="#64748b")
        draw.text((110, curr_y + 14), desc, fill="#0f172a")
        draw.text((540, curr_y + 14), qty, fill="#334155")
        draw.text((630, curr_y + 14), rate, fill="#334155")
        draw.text((750, curr_y + 14), amt, fill="#0f172a")
        curr_y += 45

    # Totals Section Box
    tot_box_top = curr_y + 40
    draw.rectangle([(width - 450, tot_box_top), (width - 50, tot_box_top + 170)], fill="#f8fafc", outline="#cbd5e1", width=1)
    
    # Subtotal
    draw.text((width - 420, tot_box_top + 20), "Subtotal (Net Amount):", fill="#334155")
    draw.text((width - 170, tot_box_top + 20), "$4,000.00", fill="#0f172a")

    # Tax
    draw.text((width - 420, tot_box_top + 55), "Sales Tax / VAT (10%):", fill="#334155")
    draw.text((width - 170, tot_box_top + 55), "$400.00", fill="#0f172a")

    # Divider
    draw.line([(width - 430, tot_box_top + 95), (width - 70, tot_box_top + 95)], fill="#cbd5e1", width=2)

    # Grand Total
    draw.text((width - 420, tot_box_top + 115), "TOTAL AMOUNT DUE:", fill="#0f172a")
    draw.text((width - 180, tot_box_top + 115), "$4,400.00", fill="#0284c7")

    # Terms & Signature footer
    draw.line([(50, height - 160), (width - 50, height - 160)], fill="#e2e8f0", width=1)
    draw.text((50, height - 140), "Payment Terms: Net 30 Days. Late payments subject to a 1.5% compounding monthly surcharge.", fill="#64748b")
    draw.text((50, height - 120), "Authorized Signature: Electronic Audit Certification Approved & Verified", fill="#10b981")
    draw.text((width - 320, height - 120), "FinSentry Test Sample Bill", fill="#94a3b8")

    img.save(output_path, "PNG", quality=95)
    print(f"Realistic test bill image generated at: {os.path.abspath(output_path)}")
    return output_path

if __name__ == "__main__":
    generate_bill_image()
