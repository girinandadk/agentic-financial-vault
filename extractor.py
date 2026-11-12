"""
FinSentry Ingestion & Multimodal Extraction Engine
Extracts invoices (PDF, Images) and Bank Statements (CSV).
Utilizes Gemma 4 (gemma4:e4b) multimodal vision via Ollama /api/generate with tesseract / heuristic fallbacks.
"""
import io
import os
import re
import csv
import json
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

import httpx
from pydantic import BaseModel, Field
from PIL import Image

import pdfplumber
import pypdf

from config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_NUM_CTX, OLLAMA_TIMEOUT
from llm_client import generate_structured_output
from db import mask_account_number

logger = logging.getLogger("finsentry.extractor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class ExtractedInvoice(BaseModel):
    vendor_name: str = Field(description="Name of the vendor/supplier")
    tax_id: Optional[str] = Field(default=None, description="Tax ID / GSTIN / VAT number")
    invoice_number: str = Field(description="Invoice identification number")
    invoice_date: str = Field(description="Date of invoice in YYYY-MM-DD")
    subtotal: float = Field(default=0.0, description="Pre-tax amount")
    tax_amount: float = Field(default=0.0, description="Total tax or VAT amount")
    total_amount: float = Field(description="Total payable amount")
    category: Optional[str] = Field(default="General Corporate Expense", description="Expense category")
    bank_account_last4: Optional[str] = Field(default=None, description="Last 4 digits or full bank account on invoice")


class BankTransactionRow(BaseModel):
    txn_date: str
    raw_narration: str
    clean_merchant: Optional[str] = None
    debit_amount: float = 0.0
    credit_amount: float = 0.0


def call_ollama_generate(prompt: str, image_b64: Optional[str] = None, format_schema: Optional[dict] = None) -> Optional[str]:
    """
    Calls Ollama /api/generate with strict settings:
    - think: False
    - num_ctx: 4096
    - deterministic temperature: 0.1
    """
    payload: Dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "temperature": 0.1,
            "think": False,
        }
    }
    if image_b64:
        payload["images"] = [image_b64]
    if format_schema:
        payload["format"] = format_schema

    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            resp = client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", "").strip()
            else:
                logger.warning(f"Ollama returned HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.info(f"Ollama call not available or failed ({e}). Falling back to local OCR/regex heuristic pipeline.")
    return None


def clean_json_markdown(raw_text: str) -> str:
    """Strips markdown code fences ```json ... ``` if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def run_ocr_tesseract(image: Image.Image) -> str:
    """Fallback OCR using pytesseract if available."""
    try:
        import pytesseract
        return pytesseract.image_to_string(image)
    except Exception as e:
        logger.warning(f"Tesseract OCR fallback skipped: {e}")
        return ""


def extract_regex_invoice(raw_text: str, filename: str) -> ExtractedInvoice:
    """Heuristic fallback regex parser for structured invoices."""
    text = raw_text.replace("\r", " ")
    
    # Vendor extraction: first non-empty header line or line with vendor identifiers
    vendor = "Unknown Vendor"
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        for line in lines[:5]:
            lower = line.lower()
            if any(term in lower for term in ["inc", "llc", "corp", "ltd", "gmbh", "technologies", "services", "solutions", "cloud", "logistics", "advisory", "security", "hosting"]):
                vendor = line
                break
            elif "vendor:" in lower or "from:" in lower or "supplier:" in lower:
                parts = re.split(r":", line, maxsplit=1)
                if len(parts) > 1 and parts[1].strip():
                    vendor = parts[1].strip()
                    break
        if vendor == "Unknown Vendor" and lines:
            vendor = lines[0]

    # Invoice Number
    inv_num_match = re.search(r"(?:invoice\s*(?:#|number|no\.?|id)|inv\s*(?:#|number|no\.?)|bill\s*(?:#|number|no\.?))\s*[:.]?\s*([A-Za-z0-9\-_/]+)", text, re.IGNORECASE)
    invoice_number = inv_num_match.group(1) if inv_num_match else f"INV-{abs(hash(filename)) % 100000:05d}"

    # Invoice Date
    date_match = re.search(r"(?:date|inv\s*date|dated)\s*[:.]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})", text, re.IGNORECASE)
    invoice_date = datetime.now().strftime("%Y-%m-%d")
    if date_match:
        raw_d = date_match.group(1).replace(".", "-").replace("/", "-")
        try:
            parts = raw_d.split("-")
            if len(parts[0]) == 4:
                invoice_date = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            else:
                invoice_date = f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
        except Exception:
            pass

    # Amounts
    # Subtotal
    subtotal = 0.0
    sub_m = re.search(r"(?:subtotal|sub-total|net\s*amount)\s*[:.]?\s*[\$€£₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if sub_m:
        try:
            subtotal = float(sub_m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Tax Amount
    tax_amount = 0.0
    tax_m = re.search(r"(?:tax|vat|gst|sales\s*tax)\s*(?:\([^)]*\))?\s*[:.]?\s*[\$€£₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if tax_m:
        try:
            tax_amount = float(tax_m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Total Amount
    total_amount = 0.0
    tot_m = re.search(r"(?:total\s*due|grand\s*total|amount\s*due|total\s*paid|balance\s*due|\btotal\b)\s*[:.]?\s*[\$€£₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if tot_m:
        try:
            total_amount = float(tot_m.group(1).replace(",", ""))
        except ValueError:
            pass

    if total_amount == 0.0 and subtotal > 0.0:
        total_amount = round(subtotal + tax_amount, 2)
    elif subtotal == 0.0 and total_amount > 0.0:
        subtotal = round(total_amount - tax_amount, 2)

    # Tax ID
    tax_id = None
    tax_id_m = re.search(r"(?:tax\s*id|gstin|vat\s*no|ein)\s*[:.]?\s*([A-Za-z0-9\-]+)", text, re.IGNORECASE)
    if tax_id_m:
        tax_id = tax_id_m.group(1).strip()

    # Bank Account
    bank_acc = None
    bank_m = re.search(r"(?:remittance\s+)?bank\s*(?:a/c|acc|account)?\s*[:.]?\s*([A-Za-z0-9\-*]+)", text, re.IGNORECASE)
    if bank_m:
        raw_b = bank_m.group(1).replace("*", "").strip()
        if raw_b:
            bank_acc = raw_b

    # Category heuristic
    cat = "General Corporate Expense"
    v_low = vendor.lower()
    if any(k in v_low for k in ["cloud", "aws", "gcp", "azure", "server", "host", "saas"]):
        cat = "Cloud & Infrastructure"
    elif any(k in v_low for k in ["office", "supplies", "stationery", "staples"]):
        cat = "Office Supplies"
    elif any(k in v_low for k in ["consulting", "advisory", "legal", "counsel"]):
        cat = "Professional Services"
    elif any(k in v_low for k in ["hardware", "dell", "apple", "laptop", "industrial", "manufacturing"]):
        cat = "Manufacturing & Hardware"
    elif any(k in v_low for k in ["logistics", "freight", "courier", "shipping"]):
        cat = "Logistics & Freight"
    elif any(k in v_low for k in ["security", "guard", "vanguard"]):
        cat = "Security Services"

    return ExtractedInvoice(
        vendor_name=vendor,
        tax_id=tax_id,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        subtotal=subtotal,
        tax_amount=tax_amount,
        total_amount=total_amount,
        category=cat,
        bank_account_last4=bank_acc[-4:] if bank_acc else None
    )


def extract_from_image(image_path: Path) -> ExtractedInvoice:
    """Extracts structured invoice from an image (.png, .jpg, .jpeg) using Gemma 4 multimodal vision or fallback."""
    try:
        with Image.open(image_path) as img:
            rgb_img = img.convert("RGB")
            buf = io.BytesIO()
            rgb_img.save(buf, format="JPEG", quality=90)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to read image {image_path}: {e}")
        return ExtractedInvoice(
            vendor_name="Unknown Vendor",
            invoice_number=f"ERR-{image_path.stem}",
            invoice_date=datetime.now().strftime("%Y-%m-%d"),
            subtotal=0.0,
            tax_amount=0.0,
            total_amount=0.0
        )

    # Prompt for Gemma 4
    prompt = (
        "You are an expert corporate financial auditor. Analyze this invoice or receipt image and extract the "
        "structured data strictly matching the requested JSON schema.\n"
        "Fields to extract:\n"
        "- vendor_name: string\n"
        "- tax_id: string or null\n"
        "- invoice_number: string\n"
        "- invoice_date: string (format YYYY-MM-DD)\n"
        "- subtotal: float\n"
        "- tax_amount: float\n"
        "- total_amount: float\n"
        "- category: string (expense category)\n"
        "- bank_account_last4: string or null (last 4 digits of vendor remittance bank account if mentioned)\n"
        "Return strictly valid JSON with no markdown and no conversational text."
    )

    json_schema = ExtractedInvoice.model_json_schema()
    llm_resp = call_ollama_generate(prompt=prompt, image_b64=img_b64, format_schema=json_schema)

    if llm_resp:
        try:
            parsed = json.loads(clean_json_markdown(llm_resp))
            return ExtractedInvoice(**parsed)
        except Exception as e:
            logger.warning(f"Failed to parse Gemma 4 image response as ExtractedInvoice: {e}. Payload: {llm_resp}")

    # Fallback to Tesseract OCR + Heuristic
    with Image.open(image_path) as img:
        ocr_text = run_ocr_tesseract(img)
    return extract_regex_invoice(ocr_text, image_path.name)


def extract_from_pdf(pdf_path: Path) -> ExtractedInvoice:
    """Extracts text from PDF via pdfplumber/pypdf and uses Gemma 4 or heuristic parsing."""
    full_text = ""
    first_page_image_b64 = None

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    full_text += t + "\n"
            
            # If text is very short (scanned PDF), try extracting the first page as an image
            if len(full_text.strip()) < 50 and len(pdf.pages) > 0:
                p0 = pdf.pages[0]
                img_obj = p0.to_image(resolution=150)
                buf = io.BytesIO()
                img_obj.original.save(buf, format="JPEG")
                first_page_image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.warning(f"pdfplumber extraction encountered error on {pdf_path}: {e}")

    # If text is present, extract via unified LLM (local Gemma 4 / Gemini API) with regex fallback
    if len(full_text.strip()) >= 50:
        prompt = (
            f"You are an autonomous corporate financial auditor. Read this invoice text and extract the exact fields:\n"
            f"- vendor_name: Exact corporate vendor name\n"
            f"- tax_id: Tax ID / GSTIN / VAT / EIN or null\n"
            f"- invoice_number: Invoice reference number\n"
            f"- invoice_date: Date formatted as YYYY-MM-DD\n"
            f"- subtotal: Pre-tax subtotal float\n"
            f"- tax_amount: Tax/VAT float\n"
            f"- total_amount: Total amount float\n"
            f"- category: Expense category (e.g. Cloud & Infrastructure, Office Supplies, Legal Services, Logistics, Hardware)\n"
            f"- bank_account_last4: Last 4 digits of vendor remittance bank account if mentioned or null\n\n"
            f"INVOICE TEXT:\n```\n{full_text[:4000]}\n```\n"
        )
        llm_extracted = generate_structured_output(prompt, ExtractedInvoice)
        if llm_extracted:
            logger.info(f"[LLM EXTRACTION SUCCESS] {pdf_path.name} -> {llm_extracted.vendor_name} (#{llm_extracted.invoice_number}) Total: ${llm_extracted.total_amount:,.2f}")
            return llm_extracted

        return extract_regex_invoice(full_text, pdf_path.name)

    # Scanned PDF path using Gemma 4 vision
    if first_page_image_b64:
        prompt = "Extract invoice JSON from this scanned PDF page."
        json_schema = ExtractedInvoice.model_json_schema()
        llm_resp = call_ollama_generate(prompt=prompt, image_b64=first_page_image_b64, format_schema=json_schema)
        if llm_resp:
            try:
                parsed = json.loads(clean_json_markdown(llm_resp))
                return ExtractedInvoice(**parsed)
            except Exception as e:
                logger.warning(f"Failed to parse LLM scanned response: {e}")

    return extract_regex_invoice(full_text, pdf_path.name)


def parse_bank_statement_csv(csv_path: Path) -> List[BankTransactionRow]:
    """Parses bank statement CSV into standardized transaction rows."""
    transactions: List[BankTransactionRow] = []
    
    with open(csv_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if not row or not any(row):
                continue
            if header is None:
                header = [c.strip().lower() for c in row]
                continue

            # Map columns
            row_dict = {}
            for i, val in enumerate(row):
                if i < len(header):
                    row_dict[header[i]] = val.strip()

            # Date
            date_col = next((row_dict[k] for k in row_dict if "date" in k and row_dict[k]), None)
            if not date_col:
                continue

            # Clean Date to YYYY-MM-DD
            clean_date = date_col.replace("/", "-").replace(".", "-")
            try:
                parts = clean_date.split("-")
                if len(parts) == 3:
                    if len(parts[0]) == 4:
                        clean_date = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                    else:
                        clean_date = f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            except Exception:
                pass

            # Narration
            narration = next((row_dict[k] for k in row_dict if any(x in k for x in ["narration", "description", "details", "memo", "particulars"]) and row_dict[k]), "Bank Transaction")

            # Debits / Credits / Amount
            debit = 0.0
            credit = 0.0

            debit_raw = next((row_dict[k] for k in row_dict if any(x in k for x in ["debit", "withdrawal", "dr"]) and row_dict[k]), None)
            credit_raw = next((row_dict[k] for k in row_dict if any(x in k for x in ["credit", "deposit", "cr"]) and row_dict[k]), None)
            amount_raw = next((row_dict[k] for k in row_dict if k in ["amount", "txn_amount", "transaction amount"] and row_dict[k]), None)

            def to_f(v):
                if not v:
                    return 0.0
                v = re.sub(r"[^\d.-]", "", v)
                try:
                    return float(v)
                except ValueError:
                    return 0.0

            if debit_raw:
                debit = abs(to_f(debit_raw))
            if credit_raw:
                credit = abs(to_f(credit_raw))

            if debit == 0.0 and credit == 0.0 and amount_raw:
                val = to_f(amount_raw)
                if val < 0:
                    debit = abs(val)
                else:
                    debit = val

            # Derive clean merchant from narration
            clean_m = re.sub(r"^(?:ACH|WIRE|NEFT|RTGS|IMPS|POS|CARD|TRANSFER|BILLDESK|RAZORPAY|STRIPE|CHECK|PAYPAL)\s*[-/:]?\s*", "", narration, flags=re.IGNORECASE).strip()

            transactions.append(BankTransactionRow(
                txn_date=clean_date,
                raw_narration=narration,
                clean_merchant=clean_m[:100],
                debit_amount=round(debit, 2),
                credit_amount=round(credit, 2)
            ))

    return transactions
