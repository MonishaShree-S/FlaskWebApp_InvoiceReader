import fitz  # PyMuPDF
import re
def extract_text_from_pdf(pdf_path):
    file=pdf_path
    doc = fitz.open(pdf_path)  # Open the PDF file
    extracted_text = ""

    for page in doc:  # Loop through all pages
        extracted_text += page.get_text("text") + "\n\n"  # Extract text
    #extracted_text = " ".join(extracted_text.split())
    print(extracted_text)
    doc.close()
    return extracted_text
def format_receipt_text(text):
    """
    Format receipt text…
    """
    # Ensure consistent spacing
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'REWE', r'REWE\n', text)

    text = re.sub(r'(Peterson Warenhandels GmbH&Co.KG)', r'\1\n', text)
    text = re.sub(r'(Schönwalder Straße \d+ \d{5} Berlin/Spandau)', r'\1\n', text)
    text = re.sub(r'(UID Nr\.: DE\d+)', r'\1\n', text)

    # Keep EUR/kg intact
    text = re.sub(r'(\d+,\d+\s+EUR)\s*/?\s*kg', r'\1/kg', text)
    text = re.sub(r'(EUR/kg)\s+([A-ZÄÖÜ].+? [\d,]+ [AB])', r'\1\n\2', text)

    # Ensure item separation at "... X,XX A|B"
    text = re.sub(r'([+-]?\d+[\s,.]\d{2})(?=\s+[A-B])\s+([A-B])', r'\1 \2\n', text)

    # Keep "price A|B" with the following "N Stk x U,UU"
    text = re.sub(r'(\d+,\d+\s+[AB])\s+(?=\d+\s+Stk\s+x\s+\d+,\d+)', r'\1 ', text)

    # --- 🔧 NEW: join price line with piece block if formatter split them
    # Converts:
    #   "* LEERG. MW V. ST -0,72 A 9 Stk x 0,08"
    # Into:
    #   "* LEERG. MW V. ST -0,72 A\n9 Stk x 0,08"
    text = re.sub(
        r'([A-ZÄÖÜa-z0-9!+&/\s\.-]+?\s+[+-]?\d+,\d{2}\s+[AB])\s+(\d+)\s+Stk\s*x\s*([+-]?\d+,\d{2})',
        r'\1\n\2 Stk x \3',
        text
    )
    text = re.sub(
        r'([+-]?\d+,\d{2}\s+[AB])\s*\*?\s*\n\s*\*?\s*(\d+)\s*Stk\s*x\s*([+-]?\d+,\d{2})',
        r'\1 \2 Stk x \3',
        text
    )

    # --- 🔧 NEW: drop a stray tax code after piece block (OCR turns “AVOCADO …” into “… 1,19 A …”)
    # e.g. "2 Stk x 1,19 A AVOCADO…" -> "2 Stk x 1,19 \nAVOCADO…"
    text = re.sub(
        r'(\d+\s*Stk\s*x\s*\d+,\d{2})\s+[AB](?=\s+[A-ZÄÖÜ])',
        r'\1\n',
        text
    )

    # Force newline before next item name after a piece block (whether or not OCR added A/B)
    # e.g. "… 2 Stk x 1,19 AVOCADO…" -> "… 2 Stk x 1,19\nAVOCADO…"
    text = re.sub(
        r'(\d+\s*Stk\s*x\s*\d+,\d{2})(?:\s+[AB])?\s+(?=[A-ZÄÖÜ])',
        r'\1\n',
        text
    )

    # Dates on their own line
    text = re.sub(r'(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})', r'\n\1\n ', text)

    # Keep weight blocks intact on the same line
    text = re.sub(
        r'(\d+,\d+\s+[AB])\s+(?=\d+,\d+\s+kg\s+x\s+\d+,\d+\s+EUR/kg)',
        r'\1 ',
        text
    )

    # If a full "price A|B + N Stk x U,UU" is followed by the next item, push the item to a new line
    text = re.sub(
        r'(\d+,\d+\s+[AB]\s+\d+\s+Stk\s+x\s+\d+,\d+)\s+([A-ZÄÖÜ].+? [\d,]+ [AB])',
        r'\1\n\2',
        text
    )
    OCR_FIXES = {
        r'\bVOCADO VORGER\.': 'AVOCADO VORGER.',
        r'\bRANATAPFEL\b': 'GRANATAPFEL',
        # add more as needed
    }
    for bad, good in OCR_FIXES.items():
        text = re.sub(bad, good, text, flags=re.I)
    # Tidy
    text = re.sub(r'\n\s+', '\n', text)
    text = re.sub(r'-{5,}', '', text)
    text = re.sub(r'={5,}', '', text)
    return text.strip()


def format_receipt_text_old(text):
    """
    Format receipt text:
    1. Fix 'EUR/kg' breaking items into the same line.
    2. Ensure each item starts on a new line when a price with 'A' or 'B' is detected.
    3. Keep '2 Stk x 0,99' together with its related item.
    4. Ensure product names (like "LAYS KRAEUTERBUT") go to a new line if they follow '2 Stk x 0,99'.

    Args:
        text (str): Raw extracted text from the receipt.

    Returns:
        str: Properly formatted receipt text.
    """

    # Ensure consistent spacing
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'REWE', r'REWE\n', text)

    text = re.sub(r'(Peterson Warenhandels GmbH&Co.KG)', r'\1\n', text)
    text = re.sub(r'(Schönwalder Straße \d+ \d{5} Berlin/Spandau)', r'\1\n', text)
    text = re.sub(r'(UID Nr\.: DE\d+)', r'\1\n', text)
    # Fix EUR/kg issue by merging split parts
    text = re.sub(r'(\d+,\d+\s+EUR)\s*/?\s*kg', r'\1/kg', text)  # Ensures EUR/kg stays together

    # Ensure that if a line ends with EUR/kg, the next item moves to a new line
    text = re.sub(r'(EUR/kg)\s+([A-ZÄÖÜ].+? [\d,]+ [AB])', r'\1\n\2', text)

    # Ensure item separation: Add a newline before price values (X,XX A or X,XX B)
    #text = re.sub(r'(\d+,\d+\s+[AB])', r'\n\1', text)  # Ensures items with price A/B start on a new line
    text = re.sub(r'([+-]?\d+[\s,.]\d{2})(?=\s+[A-B])\s+([A-B])', r'\1 \2\n', text)
    # Move new product names to a new line if they appear after '2 Stk x 0,99'
    # Keep 'Price A/B + Quantity' together on the same line
    text = re.sub(r'(\d+,\d+\s+[AB])\s+(?=\d+\s+Stk\s+x\s+\d+,\d+)', r'\1 ', text)
    # 1) Put 'N Stk x U,UU' on the NEXT line, even if a stray A/B follows it
    text = re.sub(
        r'(\d+,\d{2}\s+[AB])\s+(?=\d+\s*Stk\s*x\s*\d+,\d{2}(?:\s+[AB])?)',
        r'\1\n',
        text
    )

    # 2) Strip a stray trailing tax code after the piece-block line itself:
    #    '2 Stk x 1,19 A'  ->  '2 Stk x 1,19'
    text = re.sub(
        r'(\d+\s*Stk\s*x\s*\d+,\d{2})\s+[AB](?=\s|$)',
        r'\1',
        text
    )

    text = re.sub(r'(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})',r'\n\1\n ', text)
    # Keep 'Price A/B + Weight-based Pricing' together on the same line
    text = re.sub(r'(\d+,\d+\s+[AB])\s+(?=\d+,\d+\s+kg\s+x\s+\d+,\d+\s+EUR/kg)', r'\1 ', text)
    # Move next product name to a new line if it follows 'A/B X Stk x X,XX'
    text = re.sub(r'(\d+,\d+\s+[AB]\s+\d+\s+Stk\s+x\s+\d+,\d+)\s+([A-ZÄÖÜ].+? [\d,]+ [AB])', r'\1\n\2', text)
    # Join "… A/B [*]" line with a following "N Stk x U,UU" line (allow optional leading '*', variable spacing)
    text = re.sub(
        r'([+-]?\d+,\d{2}\s+[AB])\s*\*?\s*\n\s*\*?\s*(\d+)\s*Stk\s*x\s*([+-]?\d+,\d{2})',
        r'\1 \2 Stk x \3',
        text
    )
    # Clean up excess spaces after newline insertions
    text = re.sub(r'\n\s+', '\n', text)
    text = re.sub(r'-{5,}', '', text)  # Removes long dashes (-----)
    text = re.sub(r'={5,}', '', text)
    return text.strip()

def euro_to_float(s: str) -> float:
        # keep sign; convert "1.234,56" or "-3,99" → float
        s = s.replace('.', '').replace(',', '.')
        return float(s)


def _norm_name(s: str) -> str:
    # remove ANY occurrence of "…,XX A|B" (embedded or trailing)
    s = re.sub(r'\s+\d+,\d{2}\s+[AB](?=\s|$)', '', s)

    # remove an attached "N Stk x U,UU" block (embedded or trailing)
    s = re.sub(r'\s+\d+\s*Stk\s*x\s*\d+,\d{2}(?=\s|$)', '', s, flags=re.I)

    # also handle the case where OCR glued just the "2 Stk x" without the unit
    s = re.sub(r'\s+\d+\s*Stk\s*x\b.*$', '', s, flags=re.I)

    return re.sub(r'\s+', ' ', s).strip().lower()


def _clean_item_label(label: str) -> str:
    s = re.sub(r'\s+\d+,\d{2}\s+[AB](?=\s|$)', '', label)
    s = re.sub(r'\s+\d+\s*Stk\s*x\s*\d+,\d{2}(?=\s|$)', '', s, flags=re.I)
    s = re.sub(r'\s+\d+\s*Stk\s*x\b.*$', '', s, flags=re.I)
    return re.sub(r'\s+', ' ', s).strip()


def parse_weight_metadata(formatted_text: str):
    """
    Match:
        NAME 1,06 B
        0,180 kg x 5,90 EUR/kg
    """
    pattern = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-zäöüß0-9!+&/\s\.-]+?)\s+(?P<price>[+-]?\d+,\d{2})\s+[AB]\s*'
        r'\n\s*(?P<kg>[+-]?\d+,\d+)\s*kg\s*x\s*(?P<eurkg>[+-]?\d+,\d+)\s*EUR\s*/?\s*kg\s*$',
        re.M
    )
    table = {}
    for m in pattern.finditer(formatted_text):
        name = _norm_name(m.group('name'))
        kg = euro_to_float(m.group('kg'))
        eurkg = euro_to_float(m.group('eurkg'))
        table[name] = {"quantity": kg, "pricePerKg": round(eurkg, 2)}
    return table

def parse_items_from_formatted_text(text: str):
    """
    Returns a list of dicts like:
      {"item": str, "quantity": float, "price": float, "pricePerKg": Optional[float], "shopName": "REWE"}
    """
    items = []
    # --- [ NEW ] Fix single-line PFAND / deposit items like:
    # "* LEERG. MW V. ST -0,72 A 9 Stk x 0,08"
    # "LEERGUT EINWEG -6,50 A 26 Stk x 0,25"
    pfand_regex = re.compile(
        r'([A-ZÄÖÜa-z0-9!+&/\s\.-]+?)\s+([+-]?\d+,\d{2})\s+[AB]\s+(\d+)\s+Stk\s*x\s*([+-]?\d+,\d{2})',
        re.I
    )
    pfand_items = []
    for m in pfand_regex.finditer(text):
        name = m.group(1).strip()
        price = euro_to_float(m.group(2))  # already the TOTAL, correct negative value
        qty = float(m.group(3))
        unit = euro_to_float(m.group(4))

        pfand_items.append({
            "item": name,
            "quantity": qty,
            "price": price,  # KEEP THE NEGATIVE SIGN
            "pricePerKg": None,
            "shopName": "REWE"
        })

    # Add PFAND items to the items list FIRST, and remember them so regex below does not duplicate
    pfand_names = {i["item"].lower() for i in pfand_items}
    # 1) Grab the basic lines first (keeps original order)
    regular_item_regex = re.compile(
        r'^(?!SUMME\b|Geg\.\b|Mastercard\b|Datum\b|Uhrzeit\b|Beleg-Nr\.\b|UID\b)'
        r'([A-ZÄÖÜa-zäöüß0-9!+&/\s\.-]+?)\s+([+-]?\d+,\d{2})\s+[AB]\s*(?:\*)?\s*$',
        re.M
    )
    ordered = []
    for m in regular_item_regex.finditer(text):
        name = m.group(1).strip()
        price = euro_to_float(m.group(2))
        if 'gratis' in name.lower() and price > 0:
            price = -abs(price)
        ordered.append(name)
        items.append({
            "item": name,
            "quantity": 1.0,           # may change (weight/pieces)
            "price": round(price, 2),  # may be recomputed
            "pricePerKg": None,        # only for weighted items
            "shopName": "REWE"
        })

    # 2) Parse weight & piece metadata
    weight_info = parse_weight_metadata(text)
    piece_info  = parse_piece_metadata(text)

    # 3) Apply metadata by normalized item name
    for it in items:
        key = _norm_name(it["item"])
        it["item"] = _clean_item_label(it["item"])

        if key in weight_info:
            qty = weight_info[key]["quantity"]
            ppk = weight_info[key]["pricePerKg"]
            sign = -1 if it["price"] < 0 else 1
            it["quantity"] = qty
            it["pricePerKg"] = ppk
            it["price"] = round(sign * qty * ppk, 2)
            continue  # weight wins

        if key in piece_info:
            info = piece_info[key]
            sign = -1 if it["price"] < 0 else 1
            it["quantity"] = info["quantity"]
            # we don’t store per‑piece price in DB; keep pricePerKg None
            it["price"] = round(sign * info["quantity"] * info["unitPrice"], 2)

    return items


def parse_stk_metadata(formatted_text: str):
    """
    Find blocks like:
        PFAND 0,24 A *
        3 Stk x 0,08
    (or already joined to one line by formatter)
    and return {normalized_name: {"quantity": 3.0, "unit": 0.08}}
    """
    pattern_block = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-zäöüß0-9!+&/\s\.-]+?)\s+(?P<price>[+-]?\d+,\d{2})\s+[AB]\s*\*?\s*'
        r'(?:\n\s*\*?\s*|\s+)'  # newline (maybe) and optional leading *
        r'(?P<qty>\d+)\s*Stk\s*x\s*(?P<unit>[+-]?\d+,\d{2})\s*$',
        re.M
    )

    # also catch cases where your formatter already put them on one line:
    pattern_inline = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-zäöüß0-9!+&/\s\.-]+?)\s+(?P<price>[+-]?\d+,\d{2})\s+[AB]\s+'
        r'(?P<qty>\d+)\s*Stk\s*x\s*(?P<unit>[+-]?\d+,\d{2})\s*$',
        re.M
    )

    def euro_to_float_local(s: str) -> float:
        return float(s.replace('.', '').replace(',', '.'))

    def norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s).strip().lower()

    out = {}
    for pat in (pattern_block, pattern_inline):
        for m in pat.finditer(formatted_text):
            out[norm(m.group('name'))] = {
                "quantity": float(m.group('qty')),
                "unit": round(euro_to_float_local(m.group('unit')), 2)
            }
    return out



def parse_piece_metadata(text: str):
    """
    Bind piece-based metadata (e.g., '2 Stk x 1,19') to the *previous* item line.

    Supports:
      A) Two-line style (REWE):
         'GRANATAPFEL 2,38 B'
         ' 2 Stk x 1,19'
      B) Same-line style:
         'GRANATAPFEL 2,38 B  2 Stk x 1,19'
      C) One-line multi-buy (fallback):
         'Hot Dog 3 1,19 3,57 B'

    Returns: { normalized_item_name: {"quantity": int, "unitPrice": float} }
    """
    table = {}

    def euro_to_float_local(s: str) -> float:
        return float(s.replace('.', '').replace(',', '.'))

    # --- A) Two-line (prev priced line, then 'N Stk x U,UU') ---
    block = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-z][\wÄÖÜäöüß !+&/\.\-]*)\s+'
        r'(?P<price>[+-]?\d+,\d{2})\s+[AB]\s*$'
        r'\n\s*(?P<count>\d+)\s*Stk\s*x\s*(?P<unit>\d+,\d{2})\s*$',
        re.M
    )

    # --- B) Same-line after price ---
    inline = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-z][\wÄÖÜäöüß !+&/\.\-]*)\s+'
        r'(?P<price>[+-]?\d+,\d{2})\s+[AB]\s+'
        r'(?P<count>\d+)\s*Stk\s*x\s*(?P<unit>\d+,\d{2})\s*$',
        re.M
    )
    inline = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-z][\wÄÖÜäöüß !+&/\.\-]*)\s+'
        r'(?P<price>[+-]?\d+,\d{2})\s+[AB]\s+'
        r'(?P<count>\d+)\s*Stk\s*x\s*(?P<unit>\d+,\d{2})'
        r'(?:\s+[AB])?(?:\s+.*)?$',  # ← tolerate trailing A/B and junk
        re.M
    )

    # --- C) One-line multi-buy (qty, unit, total, then A/B) ---
    one_line = re.compile(
        r'^(?P<name>[A-ZÄÖÜa-z][\wÄÖÜäöüß !+&/\.\-]*)\s+'
        r'(?P<count>\d+)\s*(?:x|\*)?\s*(?P<unit>\d+,\d{2})\s+'
        r'(?P<total>\d+,\d{2})\s+[AB]\s*$',
        re.M
    )

    # Tiny OCR fix (optional)
    text = re.sub(r'\bufbewahrungskorb\b', 'Aufbewahrungskorb', text, flags=re.I)

    for rx in (block, inline):
        for m in rx.finditer(text):
            key = _norm_name(m.group('name'))
            table[key] = {
                "quantity": int(m.group('count')),
                "unitPrice": round(euro_to_float_local(m.group('unit')), 2)
            }

    # Fallback: handles lines like 'Hot 3 1,19 3,57 B'
    for m in one_line.finditer(text):
        key = _norm_name(m.group('name'))
        table[key] = {
            "quantity": int(m.group('count')),
            "unitPrice": round(euro_to_float_local(m.group('unit')), 2)
        }

    return table














