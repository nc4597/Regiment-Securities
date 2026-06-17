from pathlib import Path
import fitz  # PyMuPDF

pdf_path = "statements/rob_statement.pdf"

print("Step 1: Checking for PDF...")

if not Path(pdf_path).exists():
    raise FileNotFoundError(f"Could not find PDF at: {pdf_path}")

print("Step 2: PDF found.")
print("Step 3: Opening PDF...")

doc = fitz.open(pdf_path)

print(f"Step 4: PDF has {len(doc)} pages.")
print("Step 5: Extracting text...")

text = ""

for page_num, page in enumerate(doc, start=1):
    text += f"\n\n--- PAGE {page_num} ---\n\n"
    text += page.get_text("text")

Path("outputs").mkdir(exist_ok=True)
Path("outputs/extracted_statement.txt").write_text(text, encoding="utf-8")

print("Success. Extracted statement saved to outputs/extracted_statement.txt")
print("\n--- PREVIEW ---\n")
print(text[:3000])

