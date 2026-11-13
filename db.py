"""
FinSentry Database Layer
SQLite with WAL mode enabled and strict relational schema.
"""
import sqlite3
import hashlib
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from config import DB_PATH

def hash_bank_account(account_str: Optional[str]) -> Optional[str]:
    """Generates a secure SHA-256 hash of a normalized bank account string."""
    if not account_str:
        return None
    cleaned = "".join(ch for ch in str(account_str).strip() if ch.isalnum()).upper()
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

def mask_account_number(acc: Optional[str]) -> str:
    """Masks an account number for logging / safe display (e.g., ****1234)."""
    if not acc:
        return "N/A"
    cleaned = str(acc).strip()
    if len(cleaned) <= 4:
        return f"****{cleaned}"
    return f"****{cleaned[-4:]}"

@contextmanager
def get_db():
    """Context manager for SQLite connections with WAL mode and row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Creates database schema if not present."""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_files (
            file_hash TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS vendors (
            vendor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            tax_id TEXT,
            bank_account_hash TEXT,
            standard_category TEXT
        );

        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER,
            invoice_number TEXT NOT NULL,
            invoice_date DATE,
            due_date DATE,
            subtotal REAL NOT NULL DEFAULT 0.0,
            discount_amount REAL NOT NULL DEFAULT 0.0,
            cgst_amount REAL NOT NULL DEFAULT 0.0,
            sgst_amount REAL NOT NULL DEFAULT 0.0,
            igst_amount REAL NOT NULL DEFAULT 0.0,
            tax_amount REAL NOT NULL DEFAULT 0.0,
            total_amount REAL NOT NULL,
            raw_file_name TEXT,
            status TEXT DEFAULT 'PENDING',
            FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
        );

        CREATE TABLE IF NOT EXISTS bank_transactions (
            txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_date DATE,
            raw_narration TEXT NOT NULL,
            clean_merchant TEXT,
            debit_amount REAL DEFAULT 0.0,
            credit_amount REAL DEFAULT 0.0,
            matched_invoice_id INTEGER,
            match_confidence REAL DEFAULT 0.0,
            status TEXT DEFAULT 'UNMATCHED',
            FOREIGN KEY (matched_invoice_id) REFERENCES invoices(invoice_id)
        );

        CREATE TABLE IF NOT EXISTS audit_flags (
            flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_type TEXT NOT NULL,
            reference_id INTEGER NOT NULL,
            severity TEXT NOT NULL, -- CRITICAL, HIGH, MEDIUM, LOW
            issue_type TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN', -- OPEN, RESOLVED, DISMISSED
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);
        CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
        CREATE INDEX IF NOT EXISTS idx_bank_status ON bank_transactions(status);
        CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_flags(status);
        """)
        # Safe migration for existing databases
        for col_sql in [
            "ALTER TABLE invoices ADD COLUMN due_date DATE;",
            "ALTER TABLE invoices ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0.0;",
            "ALTER TABLE invoices ADD COLUMN cgst_amount REAL NOT NULL DEFAULT 0.0;",
            "ALTER TABLE invoices ADD COLUMN sgst_amount REAL NOT NULL DEFAULT 0.0;",
            "ALTER TABLE invoices ADD COLUMN igst_amount REAL NOT NULL DEFAULT 0.0;",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
