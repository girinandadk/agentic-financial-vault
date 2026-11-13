"""
FinSentry Resilient Folder Watcher
Monitors ./vault_drop via watchdog PollingObserver with file-size stability check.
Deduplicates via SHA-256 hash, archives processed files to ./vault_output/processed_archive,
extracts data, stores in DB, and triggers reconciliation.
"""
import time
import fcntl
import shutil
import hashlib
import logging
from pathlib import Path
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from config import VAULT_DROP_DIR, PROCESSED_ARCHIVE_DIR
from db import get_db, hash_bank_account, mask_account_number
from extractor import extract_from_pdf, extract_from_image, parse_bank_statement_csv
from reconciler import run_reconciliation, add_audit_flag
from agents import run_two_agent_pipeline

logger = logging.getLogger("finsentry.watcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def compute_file_sha256(file_path: Path) -> str:
    """Computes SHA-256 hash of a file for deduplication."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def wait_for_file_stability(file_path: Path, checks: int = 3, interval: float = 0.5) -> bool:
    """
    Ensures a file is fully copied / finished writing before reading.
    Verifies that file size remains constant across multiple checks.
    """
    last_size = -1
    for _ in range(checks):
        if not file_path.exists():
            return False
        try:
            curr_size = file_path.stat().st_size
            if curr_size == last_size and curr_size > 0:
                return True
            last_size = curr_size
        except OSError:
            pass
        time.sleep(interval)
    return file_path.exists() and file_path.stat().st_size > 0


def process_dropped_file(file_path: Path):
    """Processes an individual file from vault_drop with an inter-process lock to prevent duplicate ingestion."""
    if not file_path.is_file() or file_path.name.startswith("."):
        return

    name_hash = hashlib.sha256(file_path.name.encode("utf-8")).hexdigest()[:16]
    lock_path = Path(f"/tmp/finsentry_ingest_{name_hash}.lock")
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if not file_path.is_file():
                return
            _process_dropped_file_locked(file_path)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _process_dropped_file_locked(file_path: Path):
    """Locked implementation of process_dropped_file."""
    if not file_path.is_file() or file_path.name.startswith("."):
        return

    logger.info(f"Processing dropped file: {file_path.name}")
    if not wait_for_file_stability(file_path):
        logger.warning(f"File {file_path.name} was deleted or unstable during copy. Skipping.")
        return

    file_hash = compute_file_sha256(file_path)
    suffix = file_path.suffix.lower()

    # Deduplication check in DB: Only skip exact identical files if they already have an invoice record
    with get_db() as conn:
        cur = conn.execute("SELECT file_hash, file_name FROM processed_files WHERE file_hash = ?", (file_hash,))
        already_processed = cur.fetchone()
        if already_processed:
            # Check if this file actually created an invoice record previously
            existing_inv = conn.execute("SELECT invoice_id FROM invoices WHERE raw_file_name = ?", (file_path.name,)).fetchone()
            if existing_inv:
                logger.info(f"File {file_path.name} (SHA: {file_hash[:8]}...) was already audited (Inv #{existing_inv['invoice_id']}). Skipping duplicate.")
                archive_path = PROCESSED_ARCHIVE_DIR / f"duplicate_{int(time.time())}_{file_path.name}"
                shutil.move(str(file_path), str(archive_path))
                return

    # Ingest depending on doc_type
    doc_type = "UNKNOWN"
    try:
        if suffix in [".csv"]:
            doc_type = "BANK_STATEMENT"
            txns = parse_bank_statement_csv(file_path)
            with get_db() as conn:
                for t in txns:
                    conn.execute("""
                        INSERT INTO bank_transactions (txn_date, raw_narration, clean_merchant, debit_amount, credit_amount, status)
                        VALUES (?, ?, ?, ?, ?, 'UNMATCHED')
                    """, (t.txn_date, t.raw_narration, t.clean_merchant, t.debit_amount, t.credit_amount))
                logger.info(f"Ingested {len(txns)} transactions from bank statement {file_path.name}")

        elif suffix in [".pdf", ".png", ".jpg", ".jpeg"]:
            doc_type = "INVOICE"
            if suffix == ".pdf":
                extracted = extract_from_pdf(file_path)
            else:
                extracted = extract_from_image(file_path)

            with get_db() as conn:
                # 1. Manage Vendor and BEC Check
                v_cur = conn.execute("SELECT vendor_id, bank_account_hash FROM vendors WHERE name = ?", (extracted.vendor_name,))
                v_row = v_cur.fetchone()

                incoming_bank_hash = hash_bank_account(extracted.bank_account_last4)

                if v_row:
                    vendor_id = v_row["vendor_id"]
                    existing_bank_hash = v_row["bank_account_hash"]

                    # BEC FRAUD DETECTION: Check if vendor's bank account changed
                    if incoming_bank_hash and existing_bank_hash and incoming_bank_hash != existing_bank_hash:
                        add_audit_flag(
                            conn,
                            ref_type="VENDOR",
                            ref_id=vendor_id,
                            severity="CRITICAL",
                            issue_type="VENDOR_BANK_CHANGE_BEC",
                            desc=f"CRITICAL BEC ALERT: Vendor '{extracted.vendor_name}' bank account changed on Invoice {extracted.invoice_number}! Expected hash {existing_bank_hash[:10]}... but received {incoming_bank_hash[:10]}... (remittance ending in {mask_account_number(extracted.bank_account_last4)})."
                        )
                    elif incoming_bank_hash and not existing_bank_hash:
                        # Update vendor bank hash if not set
                        conn.execute("UPDATE vendors SET bank_account_hash = ? WHERE vendor_id = ?", (incoming_bank_hash, vendor_id))
                else:
                    # Insert new vendor
                    v_ins = conn.execute("""
                        INSERT INTO vendors (name, tax_id, bank_account_hash, standard_category)
                        VALUES (?, ?, ?, ?)
                    """, (extracted.vendor_name, extracted.tax_id, incoming_bank_hash, extracted.category))
                    vendor_id = v_ins.lastrowid
                    logger.info(f"Registered new vendor: {extracted.vendor_name} (ID: {vendor_id})")

                # 2. Insert Invoice
                inv_ins = conn.execute("""
                    INSERT INTO invoices (vendor_id, invoice_number, invoice_date, due_date, subtotal, line_items_sum, discount_amount, cgst_amount, sgst_amount, igst_amount, tax_amount, total_amount, raw_file_name, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """, (
                    vendor_id,
                    extracted.invoice_number,
                    extracted.invoice_date,
                    extracted.due_date,
                    extracted.subtotal,
                    extracted.line_items_sum,
                    extracted.discount_amount,
                    extracted.cgst_amount,
                    extracted.sgst_amount,
                    extracted.igst_amount,
                    extracted.tax_amount,
                    extracted.total_amount,
                    file_path.name
                ))
                invoice_id = inv_ins.lastrowid
                logger.info(f"Ingested invoice #{invoice_id}: {extracted.invoice_number} from {extracted.vendor_name} (${extracted.total_amount:,.2f})")

        # Record file as processed
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_files (file_hash, file_name, doc_type)
                VALUES (?, ?, ?)
            """, (file_hash, file_path.name, doc_type))

        # Archive processed original file safely
        archive_dest = PROCESSED_ARCHIVE_DIR / f"{int(time.time())}_{file_path.name}"
        if file_path.exists():
            try:
                shutil.move(str(file_path), str(archive_dest))
                logger.info(f"Archived {file_path.name} to {archive_dest.name}")
            except Exception as move_err:
                logger.warning(f"File {file_path.name} already moved or inaccessible: {move_err}")

        # Automatically trigger two-agent audit pipeline & reconciliation cycle
        run_two_agent_pipeline()
        run_reconciliation()

    except Exception as e:
        logger.error(f"Error processing file {file_path.name}: {e}", exc_info=True)


class VaultDropHandler(FileSystemEventHandler):
    """Watchdog event handler for vault_drop events."""
    def on_created(self, event):
        if not event.is_directory:
            process_dropped_file(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            process_dropped_file(Path(event.dest_path))


def scan_existing_files():
    """Scans and ingests any existing files sitting in vault_drop on startup."""
    for item in sorted(VAULT_DROP_DIR.iterdir()):
        if item.is_file() and not item.name.startswith("."):
            process_dropped_file(item)


def start_watcher(blocking: bool = True):
    """Starts the PollingObserver folder watcher."""
    scan_existing_files()
    event_handler = VaultDropHandler()
    observer = PollingObserver(timeout=1.0)
    observer.schedule(event_handler, str(VAULT_DROP_DIR), recursive=False)
    observer.start()
    logger.info(f"FinSentry Folder Watcher active on: {VAULT_DROP_DIR}")

    if blocking:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    return observer


if __name__ == "__main__":
    start_watcher(blocking=True)
