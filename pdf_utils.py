import fitz  # PyMuPDF
import re
from pathlib import Path

def extract_pages_from_pdf(pdf_path: Path):
    """Return list of pages (may be empty on error)."""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []  # caller will skip if no pages
    pages = []
    for page_index, page in enumerate(doc, start=1):
        try:
            text = page.get_text("text")
        except Exception:
            text = ""
        pages.append({"page_number": page_index, "text": text})
    return pages

def extract_account_number_from_text(text: str):
    patterns = [
        r"\b\d{3}\s*-\s*\d{6}\s*-\s*\d{3}\b",
        r"\b\d{3}-\d{6}-\d{3}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", "", match.group(0))
    return None

def split_pdf_into_accounts(pdf_path: Path):
    pages = extract_pages_from_pdf(pdf_path)
    if not pages:
        return []  # unreadable or empty PDF -> upstream should skip
    accounts = {}
    current_account = "Unknown"
    for page in pages:
        text = page["text"]
        page_number = page["page_number"]
        account_number = extract_account_number_from_text(text)
        if account_number:
            current_account = account_number
        if current_account not in accounts:
            accounts[current_account] = {"account_number": current_account, "pages": [], "text": ""}
        accounts[current_account]["pages"].append(page_number)
        accounts[current_account]["text"] += f"\n\n--- PAGE {page_number} ---\n\n{text}"
    return list(accounts.values())

# re-export helper used by app
def clean_rep_name_from_filename(file_name: str) -> str:
    from utils import clean_rep_name_from_filename as _f
    return _f(file_name)