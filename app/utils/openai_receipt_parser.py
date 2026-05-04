import base64
import json
from openai import OpenAI
from dotenv import load_dotenv
import os
import mimetypes

load_dotenv()

print("OPENAI_API_KEY found:", bool(os.getenv("OPENAI_API_KEY")))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
#client = OpenAI()

RECEIPT_SCHEMA_PROMPT = """
You are extracting a shopping receipt/bill from an image.

Return ONLY valid JSON (no markdown) with this structure:

{
  "shopName": string | null,
  "address": string | null,
  "invoiceNumber": string | null,
  "date": "YYYY-MM-DD" | null,
  "currency": string | null,
  "totalSum": number | null,
  "items": [
    {
      "item": string,
      "quantity": number | null,
      "weight_quantity": number | null,
      "unit_price": number | null,
      "unit_type": "piece"|"kg"|"g"|"l"|"ml"|null,
      "total_price": number | null
    }
  ]
}

Rules:
- If you are unsure, use null (not empty string).
- "quantity" is count of pieces. "weight_quantity" is weight in kg if present.
- Prefer "total_price" as the line total for the item.
"""

def parse_receipt_image_to_json(image_path: str) -> dict:
    # Guess MIME type from filename
    mime, _ = mimetypes.guess_type(image_path)
    if not mime:
        mime = "image/jpeg"  # safe fallback

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    # IMPORTANT: base64 must be provided as a DATA URL in image_url
    data_url = f"data:{mime};base64,{b64}"

    resp = client.responses.create(
        model="gpt-4.1-mini",  # or another vision-capable model
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": RECEIPT_SCHEMA_PROMPT},
                {"type": "input_image", "image_url": data_url},
            ],
        }],
    )

    return json.loads(resp.output_text.strip())