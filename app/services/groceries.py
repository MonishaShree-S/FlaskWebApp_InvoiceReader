import pandas as pd
import re
import json
import os
from app.utils.ReadImage import processImageText
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, date

import json, os, pandas as pd, re, logging

from app.services.rewe import format_receipt_text,parse_items_from_formatted_text
logger = logging.getLogger(__name__)

class ImageScanner:

    def __init__(self,imagepath,filename,extractedtext,month,year,bill_date_str):
        self.month = month
        self.year = year
        self.bill_name=filename
        self.manual_date_str=bill_date_str
        if extractedtext:
            print("++++extractedtext: \n",extractedtext)
            text_1 = 'REWE ' + extractedtext
            self.text = format_receipt_text(text_1)
            # parse items right here
            self.items = parse_items_from_formatted_text(self.text)
            print(self.text)
        else:
            self.text = processImageText(imagepath)
        #print(self.text)

    def get_text(self):
        return self.text

    def get_shop_name(self):
        # Look for the line that contains "Kaufland"
        for line in self.text.split('\n'):
            if "Kaufland" in line:
                return line.strip()
            elif "REWE" in line:
                return line.strip()
        return None



    def _to_float(self,num_str: str) -> float:
        s = (num_str or "").strip()

        # Strip currency and spaces (incl. NBSP/thin space) and normalize minus
        s = s.replace('\xa0', ' ').replace('\u202f', ' ')
        s = re.sub(r'\s+', '', s)
        s = re.sub(r'[€]', '', s)
        s = s.replace('−', '-')  # U+2212 to hyphen-minus

        # Parentheses mean negative: (12,34) -> -12,34
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]

        has_comma = ',' in s
        has_dot = '.' in s

        if has_comma and has_dot:
            # Use the rightmost separator as decimal mark
            last_comma = s.rfind(',')
            last_dot = s.rfind('.')
            if last_dot > last_comma:
                # '.' is decimal, commas are thousands
                s = s.replace(',', '')
                s = s.replace('.', '.')
            else:
                # ',' is decimal, dots are thousands
                s = s.replace('.', '')
                s = s.replace(',', '.')
        elif has_comma:
            # Only comma present -> decimal comma; drop stray thousands dots just in case
            s = s.replace('.', '')
            s = s.replace(',', '.')
        else:
            # Only dot or no separator -> dot is decimal; drop any stray commas
            s = s.replace(',', '')

        # Remove apostrophe/space thousands (e.g., 1'234.56, 1 234,56 after earlier steps)
        s = re.sub(r"[ '\u2019]", "", s)

        try:
            return float(Decimal(s))
        except InvalidOperation:
            raise ValueError(f"Unparsable amount: {num_str!r}")

    def get_Total_amount(self):
        text = self.text or ""

        # 0) Normalize whitespace (handle NBSP, NNBSP, etc.)
        text = text.replace('\xa0', ' ').replace('\u202f', ' ')
        # Don't fully collapse to one space; but do make repeated spaces simpler
        text = re.sub(r'[ \t]+', ' ', text)


        # 1) If it looks like a REWE receipt, prefer REWE total line(s)
        if 'REWE' in text:
            #m = re.search(r'\bSUMME\s+EUR\s+(\d{1,3}(?:[.,]\d{3})*,\d{2}|\d+,\d{2})\b', text, re.I)
            #if m:
                #return _to_float(m.group(1))
            m = re.search(r'\bSUMME\b(?:\s*EUR)?\s*([0-9][0-9.,\s\u202f\'-]*[0-9](?:[.,][0-9]{2})?)\b', text, re.I)
            if m:
                return self._to_float(m.group(1))

            # fallback: some REWE variants omit EUR or vary spacing/case
            m = re.search(r'\bSUMME\b\s+(?:EUR\s+)?(\d{1,3}(?:[.,]\d{3})*,\d{2}|\d+,\d{2})\b', text, re.I)
            if m:
                return self._to_float(m.group(1))

        # 2) Kaufland / generic “Summe …” (with optional EUR)
        m = re.search(r'\bSumme\b\s+(?:EUR\s+)?(\d{1,3}(?:[.,]\d{3})*,\d{2}|\d+,\d{2})\b', text, re.I)
        if m:
            return self._to_float(m.group(1))

        # 3) Payment line fallbacks seen on both (pick the amount actually charged)
        m = re.search(r'\bGeg\.\s*Mastercard\s+EUR\s+(\d{1,3}(?:[.,]\d{3})*,\d{2}|\d+,\d{2})\b', text, re.I)
        if m:
            return self._to_float(m.group(1))

        m = re.search(r'\bKartenzahlung\s+(\d{1,5}),(\d{2})\b', text, re.I)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")

        # 4) Last resort: compute from parsed items (handles 'gratis -3,99')
        items = getattr(self, "items", None) or self.get_groceries_bill()
        return round(sum(float(i.get("price", 0.0)) for i in items), 2)

    def get_Total_amount_old(self):
        # amount_pattern = r'((Total\snone\s\d+(,\d+)?)|(Total\s\d+(,\d+)?)|(Summe\s(\d+,\d+)))'
        amount_pattern = r'Summe\s(\d+,\d+)'
        amount_match = re.search(amount_pattern, self.text)
        price_pattern = r"Summe (\d+) (\d{2})"
        price_match = re.search(price_pattern, self.text)
        summe_pattern = r"Summe (\d{1,5})\s(\d{2})"
        summe_match = re.search(summe_pattern, self.text)
        # Regex pattern to capture the price from "Kartenzahlung" (with a comma)
        kartenzahlung_pattern = r"Kartenzahlung (\d{1,5}),(\d{2})"
        kartenzahlung_match = re.search(kartenzahlung_pattern, self.text)

        rewe_pattern = r"SUMME\s+EUR\s+([\d,.]+)"
        rewe_total_match = re.search(rewe_pattern, self.text)

        amount_text = None
        summe_price = None
        kartenzahlung_price = None
        if amount_match:
            amount_match = re.search(amount_pattern, self.text)
            amount_text = amount_match.group(1) if amount_match else "None"
            print('Amount_Total_Sum: ', amount_text)
        elif price_match:
            amount_text = f"{price_match.group(1)},{price_match.group(2)}"
            print("Formatted Price:", amount_text)  # Output: 100,85
        elif summe_match:
            amount_text = summe_price = f"{summe_match.group(1)},{summe_match.group(2)}"
            print("Extracted Summe Price:", amount_text)
        elif kartenzahlung_match:
            amount_text = kartenzahlung_price = f"{kartenzahlung_match.group(1)},{kartenzahlung_match.group(2)}"
            print("Extracted Kartenzahlung Price:", amount_text)
            # Compare values and return correct one
            if summe_price and kartenzahlung_price:
                if summe_price == kartenzahlung_price:
                    print("Prices match. Returning:", summe_price)
                    return summe_price
                else:
                    print("Prices do not match. Returning value from Kartenzahlung:", kartenzahlung_price)
                    return kartenzahlung_price
            elif kartenzahlung_price:
                print("Only Kartenzahlung price found. Returning:", kartenzahlung_price)
                amount_text = kartenzahlung_price
            elif summe_price:
                print("Only Summe price found. Returning:", summe_price)
                amount_text = summe_price
        elif rewe_total_match:
            amount_text = rewe_total_match.group(1) if rewe_total_match else "Not Found"
            print("Rewe total Price:", amount_text)

        if 'none' in amount_text:
            total_amount = amount_text.replace('Total none', '')
        else:
            total_amount = amount_text.replace('Total', '')
        try:
            total_amount = total_amount.replace(',', '.')
            total_amount = float(total_amount)
        except ValueError:
            total_amount = 0.0
        return total_amount

    def get_invoice(self):
        invoice_number_pattern = r"([D][E][0-9]{9})"
        invoice_number_match = re.search(invoice_number_pattern, self.text)
        rewe_invoice_number_pattern = r"Beleg-Nr\.\s+(\d+)"
        rewe_invoice_number_match = re.search(rewe_invoice_number_pattern, self.text)

        if rewe_invoice_number_match:
            invoice_number = rewe_invoice_number_match.group(1) if rewe_invoice_number_match else "Not Found"
        elif invoice_number_match:
            invoice_number = invoice_number_match.group(1) if invoice_number_match else "Not found"

        return invoice_number

    def get_TelephoneNumber(self):
        #Telefone number not present in digital receipt
        telephone_number_pattern = r"(Tel\.\s+\d+/\d*)|(Tel\.\s+[\+\d]+[\s\(\)\d]+\s+\d*)"
        telephone_number_match = re.search(telephone_number_pattern, self.text)
        telephone_number = telephone_number_match.group(0) if telephone_number_match else "Not found"
        return telephone_number

    def get_Address(self):
        # Shop names that we want to extract addresses for
        shop_names = ['Kaufland', 'REWE']

        # Identify which shop is in the receipt
        start_index = -1
        shop_name = None

        for shop in shop_names:
            if shop in self.text:
                start_index = self.text.find(shop) + len(shop)
                shop_name = shop
                break

        if start_index == -1:
            return "Shop name not found"

        # Locate the end of the address (usually before another section like Tel, UID, etc.)
        end_keywords = ["Tel", "UID"]
        end_index = len(self.text)  # Default to end of text if no keyword is found

        for keyword in end_keywords:
            found_index = self.text.find(keyword, start_index)
            if found_index != -1:
                end_index = found_index
                break

        # Extract the raw address text
        # Extract address text
        address_text = self.text[start_index:end_index].strip()

        # Split into lines and clean up spaces
        lines = [line.strip() for line in address_text.splitlines() if line.strip()]

        # Regex pattern to detect city names
        city_pattern = re.compile(
            r'\b(?:Berlin|Adlershof|Ingolstadt|Spandau|Munich|Hamburg|Cologne|Frankfurt|Stuttgart|Dusseldorf|Dresden|Leipzig|Hannover)\b',
            re.IGNORECASE
        )

        # Initialize address extraction logic
        address_lines = []
        for line in lines:
            address_lines.append(line)  # Collect all address lines
            if city_pattern.search(line):
                break  # Stop once a city name is found

        return ', '.join(address_lines)

    def get_items(self):
        Address = self.get_Address()
        start_index = self.text.find('EUR') + len('EUR') + 1  # Skip "REWE" and newline
        end_index = self.text.rfind("Total")  # Find last occurrence of "Berlin"
        # Extract the address substring
        items = self.text[start_index:end_index]
        pattern= re.compile((r'(\b[\w.-]+(\s+[\w.-]+)*\s*\d+,\d+\b)'))
        # Find all matches in the text
        matches = pattern.findall(items)
        # Create a dictionary from the matches
        result_dict = {match[0]: match[1].strip() for match in matches}

    def get_date(self):
        # date_pattern = r"Date:\s+(\d{2}.\d{2}.\d{2})"
        # date_pattern = r"Datum:\s*(\d{2}\s*\.\s*\d{2}\s*\.\s*\d{2})"
        date_pattern = r"Datum:?\s*(\d{2}\s*\.\s*\d{2}\s*\.\s*\d{2})|Datum:?\s*(\d{2}\.\d{2}\.\d{2})"
        date_pattern_1 = r"Datum (\d{2}) (\d{2})\.(\d{2})"
        rewe_date_pattern = r"(\d{2}\.\d{2}\.\d{4})"
        date_match = re.search(date_pattern, self.text)
        date_match_1 = re.search(date_pattern_1, self.text)
        rewe_date_match = re.search(rewe_date_pattern, self.text)

        if rewe_date_match:
            date = rewe_date_match.group(1) if rewe_date_match else "Not Found"
            return date
        elif date_match_1:
            # Replace the matched format with "07.10.24"
            formatted_date = f"{date_match_1.group(1)}.{date_match_1.group(2)}.{date_match_1.group(3)}"
            print("Extracted Date:", formatted_date)
            try:
                normalized_date = re.sub(r'\.\s+', '.', formatted_date)
                print("Normalized Date:", normalized_date)
                return normalized_date
            except Exception as e:
                print("Error processing date:", e)
        elif date_match:
            print(' Date object: ', date_match)
            try:
                # Normalize the date based on the matched group
                if date_match.group(1):  # If the first group matches
                    normalized_date = re.sub(r'\.\s+', '.', date_match.group(1))
                    print("Normalized Date (group 1):", normalized_date)
                    return normalized_date
                elif date_match.group(2):  # If the second group matches
                    normalized_date = date_match.group(2)
                    print("Normalized Date (group 2):", normalized_date)
                    return normalized_date
            except Exception as e:
                print("Error processing date:", e)

        else:
            print('No date match found')
            return None

    def get_groceries_bill(self):
        IsNoneItemMatch = False
        Address = self.get_Address()
        start_index = self.text.find('EUR') + len('EUR') + 1  # Skip "REWE" and newline
        # end_index = self.text.rfind("Total")  # Find last occurrence of "Berlin"

        match = re.search(r'Summe|SUMME', self.text, re.IGNORECASE)  # Case-insensitive search
        if match:
            end_index = match.start()  # Get position of the match
        else:
            end_index = -1
        # Extract the address substring
        items = self.text[start_index:end_index]
        lines = items.splitlines()

        result_list = []

        # ---------- Added helper + Kaufland-specific patterns (do not remove/alter existing code) ----------
        def euro_to_float(s: str) -> float:
            return float(s.replace('.', '').replace(',', '.'))

        kaufland_qty_line = re.compile(
            r'^(.+?)\s+(\d+)\s+(\d+,\d{2})\s+(\d+,\d{2})\s+([A-B])$',
            re.IGNORECASE
        )
        # e.g. "Bananen 0,908 kg 0,72 B" or "Äpfel 1,210 kg x 2,49 EUR/kg 3,01 B"
        kaufland_weight_line = re.compile(
            r'^(.+?)\s+([\d.,]+)\s*kg(?:\s*x\s*([\d.,]+)\s*EUR\s*/?\s*kg)?\s+([\d.,]+)\s+([A-B])$',
            re.IGNORECASE
        )
        # Simple single-line item: "K.Mini-Waffelhorn. 1,99 B"
        kaufland_single_line = re.compile(
            r'^(.+?)\s+([+-]?\d+,\d{2})\s+([A-B])$',
            re.IGNORECASE
        )
        # ---------------------------------------------------------------------------------------------------

        # Second Approach
        for i,line in enumerate(lines):
            logger.info("***************Inside groceries method")

            # ---------- Kaufland-first pass (inserted before your existing checks) ----------
            ls = line.strip()
            m_qty = kaufland_qty_line.match(ls)
            if m_qty:
                item = m_qty.group(1).strip()
                quantity = int(m_qty.group(2))
                unit_price = euro_to_float(m_qty.group(3))
                total = euro_to_float(m_qty.group(4))
                result_list.append({
                    "item": item,
                    "quantity": quantity,
                    "unit_price": round(unit_price, 2),
                    "price": round(total, 2)
                })
                continue

            m_w = kaufland_weight_line.match(ls)
            if m_w:
                item = m_w.group(1).strip()
                kg = euro_to_float(m_w.group(2))
                price_per_kg = euro_to_float(m_w.group(3)) if m_w.group(3) else None
                total = euro_to_float(m_w.group(4))
                entry = {
                    "item": item,
                    "quantity": round(kg, 3),
                    "price": round(total, 2)
                }
                if price_per_kg is not None:
                    entry["pricePerKg"] = round(price_per_kg, 2)
                result_list.append(entry)
                continue

            m_single = kaufland_single_line.match(ls)
            if m_single:
                item = m_single.group(1).strip()
                total = euro_to_float(m_single.group(2))
                # "gratis" safeguard (keep your convention)
                if 'gratis' in item.lower() and total > 0:
                    total = -abs(total)
                result_list.append({
                    "item": item,
                    "quantity": 1,
                    "price": round(total, 2)
                })
                continue
            # --------------------------------------------------------------------------------

            # First pattern to match (your original code)
            signed_price_regex = re.compile(r'^(.+?)\s+([+-]?\d+,\d{2})\s+([A-B])$', re.IGNORECASE)

            m = signed_price_regex.match(line.strip())
            if m:
                item = m.group(1).strip()
                price = float(m.group(2).replace(',', '.'))

                # If OCR loses the minus but "gratis" is present, force negative
                if 'gratis' in item.lower() and price > 0:
                    price = -abs(price)

                result_list.append({
                    "item": item,
                    "quantity": 1,
                    "price": price
                })
                continue

            pattern1 = re.compile(r'^(.+?)\s*(?:(\d+)\s*\*\s*\d+,\d+|\d+,\d+)\s*(\d+,\d+)\s+([A-B])$')
            pattern3 = re.compile(r'^(.+?)\s*(?:x?(\d+))?\s*(\d\s\d{2})\s+([A-B])$')
            pattern_k_card_rabatt = re.compile(
                r'^( K Card Rabatt)\s+(-?\d+,\d+)$')  # New pattern for K Card Rabatt check

            item_pattern_match1 = re.search(pattern1, line)
            item_pattern_match3 = re.search(pattern3, line)
            k_card_match = re.search(pattern_k_card_rabatt, line)

            weightpatternRewe = re.compile(
                r'(.+?)\s*(?:x(\d+))?\s*(\d+,\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*([\d,.]+)\s*kg\s*x\s*([\d,.]+)\s*EUR\/kg',
                re.IGNORECASE)
            weightpatternRewe_match = re.search(weightpatternRewe, line)
            weight_next_line_pattern = re.compile(
                r'([\d.,]+)\s*kg\s*x\s*([\d.,]+)\s*EUR\s*/?\s*kg',
                re.IGNORECASE
            )

            if k_card_match:
                item = k_card_match.group(1).strip()
                quantity = 1
                price = float(k_card_match.group(2).replace(',', '.'))
                if price > 0:
                    price = price * -1
                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)

            if item_pattern_match1:
                item = item_pattern_match1.group(1).strip()
                quantity = int(item_pattern_match1.group(2)) if item_pattern_match1.group(2) else 1
                price = float(item_pattern_match1.group(3).replace(',', '.'))

                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)

            elif item_pattern_match3:
                item = item_pattern_match3.group(1).strip()
                quantity = int(item_pattern_match3.group(2)) if item_pattern_match3.group(2) else 1

                # Capture the price and replace any spaces with commas
                price_text = item_pattern_match3.group(3).replace(" ", ".")
                price = float(price_text)
                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)

            elif weightpatternRewe_match:
                logger.info("Inside weightpatternRewe_match")

                item = weightpatternRewe_match.group(1).strip()
                quantity = float(
                    weightpatternRewe_match.group(6).replace(',', '.')) if weightpatternRewe_match.group(6) else 1
                logger.info(f"quantity: {quantity}")
                price = float(weightpatternRewe_match.group(3).replace(',', '.'))
                logger.info(f"price: {price}")
                unit_price = float(weightpatternRewe_match.group(7).replace(',', '.'))
                logger.info(f"unit_price: {unit_price}")

                if (not quantity or quantity == 1.0) and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    m_next = weight_next_line_pattern.search(next_line)
                    if m_next:
                        quantity = float(m_next.group(1).replace(',', '.'))
                        unit_price = float(m_next.group(2).replace(',', '.'))
                        logger.info(f"Found next-line weight: weight_quantity={quantity}, pricePerKg={unit_price}")

                result_dict = {
                    "item": item,
                    "weight_quantity": quantity,
                    "pricePerKg": unit_price,
                    "price": price
                }
                result_list.append(result_dict)

            else:
                # If first pattern doesn't match, check the second pattern
                logger.info("Inside else")
                pattern2 = re.compile(r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])$')
                item_pattern_match2 = re.search(pattern2, line)
                weightpatternRewe = re.compile(
                    r'(.+?)\s*(?:x(\d+))?\s*(\d+,\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*([\d,.]+)\s*kg\s*x\s*([\d,.]+)\s*EUR\/kg',
                    re.IGNORECASE)
                # r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*\s*([\d,.]+)\s*kg x\s*([\d,.]+)\s*EUR/kg'))
                weightpatternRewe_match = re.search(weightpatternRewe, line)
                stuckpatternRewe = re.compile(r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])\s*((\d*) Stk x (\d+,\d+))')
                stuckpatternRewe_match = re.search(stuckpatternRewe, line)

                if item_pattern_match2:
                    logger.info("Inside item_pattern_match2")
                    item = item_pattern_match2.group(1).strip()
                    quantity = int(item_pattern_match2.group(2)) if item_pattern_match2.group(2) else 1
                    price = float(item_pattern_match2.group(3).replace(',', '.'))

                    result_dict = {
                        "item": item,
                        "quantity": quantity,
                        "price": price
                    }
                    result_list.append(result_dict)

                elif weightpatternRewe_match:
                    logger.info("Inside weightpatternRewe_match")
                    item = weightpatternRewe_match.group(1).strip()
                    quantity = float(
                        weightpatternRewe_match.group(6).replace(',', '.')) if weightpatternRewe_match.group(6) else 1
                    logger.info(f"quantity: {quantity}")
                    price = float(weightpatternRewe_match.group(3).replace(',', '.'))
                    logger.info(f"price: {price}")
                    unit_price = float(weightpatternRewe_match.group(7).replace(',', '.'))
                    logger.info(f"unit_price: {unit_price}")
                    if (not quantity or quantity == 1.0) and i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        m_next = weight_next_line_pattern.search(next_line)
                        if m_next:
                            quantity = float(m_next.group(1).replace(',', '.'))
                            unit_price = float(m_next.group(2).replace(',', '.'))
                            logger.info(f"Found next-line weight: weight_quantity={quantity}, pricePerKg={unit_price}")
                    result_dict = {
                        "item": item,
                        "weight_quantity": quantity,
                        "pricePerKg": unit_price,
                        "price": price
                    }
                    result_list.append(result_dict)

                elif stuckpatternRewe_match:
                    item = stuckpatternRewe_match.group(1).strip()
                    quantity = float(stuckpatternRewe_match.group(6).replace(',', '.')) if stuckpatternRewe_match.group(
                        6) else 1
                    price = float(stuckpatternRewe_match.group(3).replace(',', '.'))
                    unit_price = float(stuckpatternRewe_match.group(7).replace(',', '.'))
                    result_dict = {
                        "item": item,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "price": price
                    }
                    result_list.append(result_dict)
        return result_list
        # Convert the list of dictionaries to JSON
        result_json = json.dumps(result_list, indent=2)

    def get_groceries_bill__(self):
        IsNoneItemMatch = False
        Address = self.get_Address()
        start_index = self.text.find('EUR') + len('EUR') + 1  # Skip "REWE" and newline
        # end_index = self.text.rfind("Total")  # Find last occurrence of "Berlin"

        match = re.search(r'Summe|SUMME', self.text, re.IGNORECASE)  # Case-insensitive search
        if match:
            end_index = match.start()  # Get position of the match
        else:
            end_index = -1
        # Extract the address substring
        items = self.text[start_index:end_index]
        lines = items.splitlines()

        result_list = []

        # Second Approach
        for line in lines:
            logger.info("***************Inside groceries method")
            # First pattern to match
            signed_price_regex = re.compile(r'^(.+?)\s+([+-]?\d+,\d{2})\s+([A-B])$', re.IGNORECASE)

            m = signed_price_regex.match(line.strip())
            if m:
                item = m.group(1).strip()
                price = float(m.group(2).replace(',', '.'))

                # If OCR loses the minus but "gratis" is present, force negative
                if 'gratis' in item.lower() and price > 0:
                    price = -abs(price)

                result_list.append({
                    "item": item,
                    "quantity": 1,
                    "price": price
                })
                continue
            pattern1 = re.compile(r'^(.+?)\s*(?:(\d+)\s*\*\s*\d+,\d+|\d+,\d+)\s*(\d+,\d+)\s+([A-B])$')
            pattern3 = re.compile(r'^(.+?)\s*(?:x?(\d+))?\s*(\d\s\d{2})\s+([A-B])$')
            pattern_k_card_rabatt = re.compile(
                r'^( K Card Rabatt)\s+(-?\d+,\d+)$')  # New pattern for K Card Rabatt check
            # pattern_rewe  = re.findall(r'([A-ZÄÖÜa-zäöüß\s.-]+)\s+([\d,.]+)\s+[AB]', self.text)
            # pattern_rewe = re.findall(r'([A-ZÄÖÜa-zäöüß\s.-]+)\s+([\d,.]+)\s+[AB]', self.text)
            item_pattern_match1 = re.search(pattern1, line)
            item_pattern_match3 = re.search(pattern3, line)
            k_card_match = re.search(pattern_k_card_rabatt, line)
            weightpatternRewe = re.compile(
                r'(.+?)\s*(?:x(\d+))?\s*(\d+,\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*([\d,.]+)\s*kg\s*x\s*([\d,.]+)\s*EUR\/kg',
                re.IGNORECASE)
            # re.compile(
            # r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*\s*([\d,.]+)\s*kg x\s*([\d,.]+)\s*EUR/kg'))
            weightpatternRewe_match = re.search(weightpatternRewe, line)
            if k_card_match:
                item = k_card_match.group(1).strip()
                quantity = 1
                price = float(k_card_match.group(2).replace(',', '.'))
                if price > 0:
                    price = price * -1
                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)

            if item_pattern_match1:
                item = item_pattern_match1.group(1).strip()
                quantity = int(item_pattern_match1.group(2)) if item_pattern_match1.group(2) else 1
                price = float(item_pattern_match1.group(3).replace(',', '.'))

                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)

            elif item_pattern_match3:
                item = item_pattern_match3.group(1).strip()
                quantity = int(item_pattern_match3.group(2)) if item_pattern_match3.group(2) else 1

                # Capture the price and replace any spaces with commas
                price_text = item_pattern_match3.group(3).replace(" ", ".")
                price = float(price_text)
                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "price": price
                }
                result_list.append(result_dict)
            elif weightpatternRewe_match:
                logger.info("Inside weightpatternRewe_match")
                item = weightpatternRewe_match.group(1).strip()
                quantity = float(
                    weightpatternRewe_match.group(6).replace(',', '.')) if weightpatternRewe_match.group(6) else 1
                logger.info(f"quantity: {quantity}")
                price = float(weightpatternRewe_match.group(3).replace(',', '.'))
                logger.info(f"price: {price}")
                unit_price = float(weightpatternRewe_match.group(7).replace(',', '.'))
                logger.info(f"unit_price: {unit_price}")
                result_dict = {
                    "item": item,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "price": price
                }
                result_list.append(result_dict)


            else:
                # If first pattern doesn't match, check the second pattern
                logger.info("Inside else")
                pattern2 = re.compile(r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])$')
                item_pattern_match2 = re.search(pattern2, line)
                weightpatternRewe = re.compile(r'(.+?)\s*(?:x(\d+))?\s*(\d+,\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*([\d,.]+)\s*kg\s*x\s*([\d,.]+)\s*EUR\/kg',re.IGNORECASE)
                    #re.compile(
                    #r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])\s*([A-ZÄÖÜa-zäöüß\s.-]+)\s*\s*([\d,.]+)\s*kg x\s*([\d,.]+)\s*EUR/kg'))
                weightpatternRewe_match = re.search(weightpatternRewe, line)
                stuckpatternRewe = re.compile(r'^(.+?)\s*(?:x(\d+))?\s*(\d+.\d+)\s+([A-D])\s*((\d*) Stk x (\d+,\d+))')
                stuckpatternRewe_match = re.search(stuckpatternRewe, line)

                if item_pattern_match2:
                    logger.info("Inside item_pattern_match2")
                    item = item_pattern_match2.group(1).strip()
                    quantity = int(item_pattern_match2.group(2)) if item_pattern_match2.group(2) else 1
                    price = float(item_pattern_match2.group(3).replace(',', '.'))

                    result_dict = {
                        "item": item,
                        "quantity": quantity,
                        "price": price
                    }
                    result_list.append(result_dict)
                elif weightpatternRewe_match:
                    logger.info("Inside weightpatternRewe_match")
                    item = weightpatternRewe_match.group(1).strip()
                    quantity = float(
                        weightpatternRewe_match.group(6).replace(',', '.')) if weightpatternRewe_match.group(6) else 1
                    logger.info(f"quantity: {quantity}")
                    price = float(weightpatternRewe_match.group(3).replace(',', '.'))
                    logger.info(f"price: {price}")
                    unit_price = float(weightpatternRewe_match.group(7).replace(',', '.'))
                    logger.info(f"unit_price: {unit_price}")
                    result_dict = {
                        "item": item,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "price": price
                    }
                    result_list.append(result_dict)

                elif stuckpatternRewe_match:
                    item = stuckpatternRewe_match.group(1).strip()
                    quantity = float(stuckpatternRewe_match.group(6).replace(',', '.')) if stuckpatternRewe_match.group(
                        6) else 1
                    price = float(stuckpatternRewe_match.group(3).replace(',', '.'))
                    unit_price = float(stuckpatternRewe_match.group(7).replace(',', '.'))
                    result_dict = {
                        "item": item,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "price": price
                    }
                    result_list.append(result_dict)
        return result_list
        # Convert the list of dictionaries to JSON
        result_json = json.dumps(result_list, indent=2)



    def create_CSV(self):
            df = pd.read_json(r'static\json\{}.json'.format(self.bill_name))
            df['index'] = df.index
            df['index'] += 1
            df.set_index('index', inplace=True)
            print('+++++++Create_CSV_METHOD+++++',df)
            return df
            #df.to_csv(r'Bills_CSV\{}.csv'.format(self.bill_name), encoding='utf-8', index=False)

    # utils_dates.py
    import re
    from datetime import datetime

    DATE_PATTERNS = [
        # e.g., "Datum 23 08 . 25" | "23 08. 25" | "23.08.2025" | "23 . 08 . 25"
        r'(?<!\d)(\d{1,2})\s*[.\s]\s*(\d{1,2})\s*[.\s]\s*(\d{2,4})(?!\d)',
        # Weird OCR with swapped separators like "23 08. 25 20 : 58"
        r'Datum\s*[:\-]?\s*(\d{1,2})\s*[.\s]\s*(\d{1,2})\s*[.\s]\s*(\d{2,4})',
    ]

    @staticmethod
    def normalize_ymd(d, m, y):
        d, m, y = int(d), int(m), int(y)
        if y < 100:  # OCR gives '25' for 2025
            y += 2000
        return datetime(y, m, d).strftime('%Y-%m-%d')

    def parse_date_from_text(self,text: str) -> str | None:
        if not isinstance(text, str) or not text.strip():
            return None
        for pat in self.DATE_PATTERNS:
            for m in re.finditer(pat, text):
                d, mth, y = m.groups()
                try:
                    return self.normalize_ymd(d, mth, y)
                except ValueError:
                    continue
        return None

    def parse_date_from_billname(self,name: str) -> str | None:
        # K_23_08_2025(.pdf) -> 2025-08-23
        if not isinstance(name, str):
            return None
        m = re.search(r'(\d{1,2})[._-](\d{1,2})[._-](\d{2,4})', name)
        if not m:
            return None
        try:
            return self.normalize_ymd(*m.groups())
        except ValueError:
            return None


    def parse_date_str(self,s: str) -> date | None:
        DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y")
        if not s:
            return None
        s = s.strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def choose_bill_date(self, raw_text: str) -> tuple[date | None, bool, date | None]:
        """
        Returns: (chosen_date, mismatch_flag, ocr_date)
        Rule: If OCR/filename has a date -> use it.
              Else, if user picked a date -> use that.
        """
        ocr_str = self.parse_date_from_text(raw_text) or self.parse_date_from_billname(self.bill_name)
        ocr_date = self.parse_date_str(ocr_str)
        user_date = self.parse_date_str(self.manual_date_str)

        # Priority: bill (OCR) first
        if ocr_date:
            mismatch = (user_date is not None and user_date != ocr_date)
            return ocr_date, mismatch, ocr_date
        # Fallback: user-picked date if bill didn’t contain one
        if user_date:
            return user_date, False, None
        # Last resort: None (let the caller decide any final fallback)
        return None, False, ocr_date

    def create_data_frame_old(self):
        logger.info("++++++: inside create_df")
        shop_name = self.get_shop_name()
        address = self.get_Address()
        invoice_number = self.get_invoice()
        items = self.get_groceries_bill()
        logger.info(f"++++++: inside create_df {items}")

        # Normalize items
        norm_items = []
        for item in items or []:
            logger.info(f"++++++: for {item} in {items}")
            # Ensure keys exist
            name = item.get("item")
            qty = item.get("quantity", 1)
            price = item.get("price")
            ppk = item.pop("unit_price", item.get("pricePerKg", None))
            # Cast/clean types (avoid NaN leaking into JSON)
            try:
                qty = float(qty) if qty is not None else None
            except Exception:
                qty = None
            try:
                price = float(price) if price is not None else None
            except Exception:
                price = None
            try:
                ppk = float(ppk) if ppk is not None else None
            except Exception:
                ppk = None

            norm_items.append({
                "item": name,
                "quantity": qty,
                "price": price,
                "pricePerKg": ppk,
            })

        total_sum = self.get_Total_amount()
        # date normalization + fallback
        raw_date = self.get_date()
        date_str = None
        if isinstance(raw_date, str) and raw_date.strip():
            date_str = raw_date.strip()
        else:
            date_str = self.infer_date_from_bill_id(self.bill_name) or datetime.now().strftime('%Y-%m-%d')

        bill_id = self.bill_name
        # Ensure month/year are strings for os.path.join and JSON
        month = str(self.month).strip().lower() if self.month is not None else None
        year = str(self.year).strip() if self.year is not None else (date_str[:4] if date_str else None)

        result_dict = {
            "shopName": shop_name,
            "address": address,
            "invoiceNumber": invoice_number,
            "totalSum": total_sum,
            "items": norm_items,
            "date": date_str,
            "billId": bill_id,
            "month": month,
            "year": year,
        }

        # Save JSON
        base_directory = "static/json"
        json_folder = os.path.join(base_directory, year or "unknown", month or "unknown")
        os.makedirs(json_folder, exist_ok=True)
        json_filename = f"{bill_id}.json" if bill_id else f"bill_{date_str}.json"
        json_path = os.path.join(json_folder, json_filename)
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(result_dict, indent=2, ensure_ascii=False))

        # DataFrame (keep numerics numeric; strings everywhere else)
        df = pd.DataFrame(norm_items, columns=['item', 'quantity', 'price', 'pricePerKg'])
        df['shopName'] = shop_name
        df['address'] = address
        df['invoiceNumber'] = invoice_number
        df['totalSum'] = pd.to_numeric(total_sum, errors='coerce')
        df['date'] = date_str
        df['month'] = month
        df['year'] = year
        df['billId'] = bill_id

        # Clean numeric columns
        for c in ['quantity', 'price', 'totalSum', 'pricePerKg']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')

        logger.info("DataFrame created successfully for user ID %s", getattr(self, "user_id", "unknown"))
        logger.debug("Extracted DataFrame:\n%s", df)
        return df

    def create_data_frame_old1(self):
        # Extracting data
        logger.info("++++++: inside create_df")
        shop_name = self.get_shop_name()
        address = self.get_Address()
        invoice_number = self.get_invoice()
        items = self.get_groceries_bill()
        # Normalize pricePerKg key if present in items
        logger.info(f"++++++: inside create_df {items}")
        for item in items:
            logger.info(f"++++++: for {item} in {items}")
            if "unit_price" in item:
                logger.info("++++++unit_price is present: inside create_df")
                item["pricePerKg"] = item.pop("unit_price")
            elif "pricePerKg" not in item:
                item["pricePerKg"] = None

        total_sum = self.get_Total_amount()
        date = self.get_date()
        bill_id = self.bill_name
        month = self.month
        year = self.year
        # Construct JSON object
        result_dict = {
            "shopName": shop_name,
            "address": address,
            "invoiceNumber": invoice_number,
            "totalSum": total_sum,
            "items": items,
            "date":date,
            "billId":bill_id,
            "month":month,
            "year":year
        }

        result_json = json.dumps(result_dict, indent=2)
        base_directory = "static/json/"
        json_folder = os.path.join(base_directory, self.year,self.month)
        if not os.path.exists(json_folder):
            os.makedirs(json_folder)
        # Save JSON to file
        json_filename = f"{self.bill_name}.json"
        json_path = os.path.join(json_folder, json_filename)  # Specify your folder path here
        with open(json_path, "w") as json_file:
            json_file.write(result_json)
        # Construct DataFrame
        df = pd.DataFrame(items, columns=['item', 'quantity', 'price', 'pricePerKg'])
        df['shopName'] = shop_name
        df['address'] = address
        df['invoiceNumber'] = invoice_number
        df['totalSum'] = total_sum
        df['date'] = date
        df['month'] = month
        df['year'] = year
        df['billId'] = bill_id
        num_cols = ['quantity', 'price', 'totalSum', 'pricePerKg']
        for c in num_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')  # '' → NaN
        print('**** create_data_frame ****',df)
        logger.info(df)
        return df

    # put this method in the same class (e.g., ImageScanner/ReweParser)
    def _normalize_item_prices(self, items: list[dict]) -> list[dict]:
        """
        Normalize parsed items without changing the original parsing logic.
        - If pricePerKg is present (weight items), keep it and null unit_price.
        - Else if unit_price present (piece items), keep it and null pricePerKg.
        - Else set both keys to None so downstream code can rely on their presence.
        """
        normalized = []
        for it in items:
            row = dict(it)  # copy; don't mutate original

            has_ppk = "pricePerKg" in row and row["pricePerKg"] is not None
            has_unit = "unit_price" in row and row["unit_price"] is not None

            if has_ppk:
                # treat as weight-based: ensure unit_price is not used downstream
                row["unit_price"] = None
            elif has_unit:
                # treat as piece-based: ensure pricePerKg is not used downstream
                row["pricePerKg"] = None
            else:
                # neither present – enforce keys for schema stability
                row.setdefault("unit_price", None)
                row.setdefault("pricePerKg", None)

            # Round numbers (nice to have; safe)
            if isinstance(row.get("price"), float):
                row["price"] = round(row["price"], 2)
            if isinstance(row.get("pricePerKg"), float):
                row["pricePerKg"] = round(row["pricePerKg"], 2)
            if isinstance(row.get("unit_price"), float):
                row["unit_price"] = round(row["unit_price"], 2)
            if isinstance(row.get("quantity"), float):
                # keep kg precision a bit higher
                row["quantity"] = round(row["quantity"], 3)

            normalized.append(row)

        return normalized

    def create_data_frame(self):
        logger.info("++++++: inside create_df")
        shop_name = self.get_shop_name()
        address = self.get_Address()
        invoice_number = self.get_invoice()
        try:
            items = self.get_groceries_bill() or []
        except Exception as e:
            logger.info("Error in calling get_groceries_bill()",e)
        logger.info(f"++++++: inside create_df {items}")


        # Normalize item dicts and pricePerKg
        #for it in items:
            #if "unit_price" in it:
                #it["pricePerKg"] = it.pop("unit_price")
            #elif "pricePerKg" not in it:
                #it["pricePerKg"] = None

        # 🔧 normalize once, no parser changes required
        items = self._normalize_item_prices(items)
        #items = [dict(x) for x in items if isinstance(x, dict)]
        items = [row for row in items if isinstance(row, dict)]
        # --- Harmonize quantity vs weight_quantity, and ensure None instead of missing ---
        for row in items:
            # ensure keys exist
            row.setdefault("quantity", None)
            row.setdefault("weight_quantity", None)
            row.setdefault("pricePerKg", None)

            is_weight = row.get("pricePerKg") not in (None, "", 0) or row.get("weight_quantity") not in (None, "")

            if is_weight:
                # weight-based line: quantity should be NULL (not NaN), weight_quantity holds kg
                row["quantity"] = None
                # if we only had "quantity" (legacy weight in quantity), move it over
                if row["weight_quantity"] in (None, "") and isinstance(row.get("quantity"), (int, float)):
                    row["weight_quantity"] = row["quantity"]
                    row["quantity"] = None
            else:
                # piece-based line: weight_quantity should be NULL
                row["weight_quantity"] = None
        total_sum = self.get_Total_amount()

        # ---- robust date handling
        raw_text = getattr(self, "formatted_text", None) or getattr(self, "ocr_text", None)
        date_str = self.parse_date_from_text(raw_text) or self.parse_date_from_billname(self.bill_name) \
                   or datetime.now().strftime("%Y-%m-%d")
        # ----

        bill_id = self.bill_name
        month = str(self.month).strip().lower() if self.month is not None else ""
        year = str(self.year).strip() if self.year is not None else date_str[:4]
        chosen_date, mismatch, ocr_date = self.choose_bill_date(raw_text)

        # Final fallback if both are missing (rare): use today, but you can also raise an error
        if not chosen_date:
            chosen_date = datetime.utcnow().date()
            # Use ISO string for any JSON usage
        chosen_date_iso = chosen_date.isoformat()  # 'YYYY-MM-DD'
        result_dict = {
            "shopName": shop_name,
            "address": address,
            "invoiceNumber": invoice_number,
            "totalSum": total_sum,
            "items": items,
            "date": chosen_date_iso,  # <-- guaranteed string
            "billId": bill_id,
            "month": month,
            "year": year,
        }

        base_directory = "static/json"
        json_folder = os.path.join(base_directory, year or "unknown", month or "unknown")
        os.makedirs(json_folder, exist_ok=True)
        json_filename = f"{bill_id}.json" if bill_id else f"bill_{date_str}.json"
        with open(os.path.join(json_folder, json_filename), "w", encoding="utf-8") as f:
            json.dump(result_dict, f, indent=2, ensure_ascii=False)

        # DataFrame
        df = pd.DataFrame(items, columns=['item', 'quantity','weight_quantity', 'price', 'pricePerKg'])
        df['shopName'] = shop_name
        df['address'] = address
        df['invoiceNumber'] = invoice_number
        # total_sum might be "5,23" → make it dot-decimal before to_numeric
        if isinstance(total_sum, str):
            total_sum_clean = total_sum.replace(',', '.')
        else:
            total_sum_clean = total_sum
        df['totalSum'] = pd.to_numeric(total_sum_clean, errors='coerce')
        #df['totalSum'] = pd.to_numeric(total_sum, errors='coerce')
        df['date'] = chosen_date  # <-- guaranteed string
        df['month'] = month
        df['year'] = year
        df['billId'] = bill_id
        for c in ['quantity', 'weight_quantity','price', 'totalSum', 'pricePerKg']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        # 2) SPECIAL: if quantity is NaN but we have weight_quantity → use that
        if 'quantity' in df.columns and 'weight_quantity' in df.columns:
            df['quantity'] = df['quantity'].fillna(df['weight_quantity'])

        # 3) finally, turn any remaining NaN into None so SQL gets NULL
        # 3) NOW force numeric columns to object so None stays None
        for c in ['quantity', 'weight_quantity', 'price', 'totalSum', 'pricePerKg']:
            if c in df.columns:
                df[c] = df[c].astype(object)
                df[c] = df[c].where(pd.notnull(df[c]), None)

        logger.info("DataFrame created successfully")
        logger.debug("Extracted DataFrame:\n%s", df)
        return df

