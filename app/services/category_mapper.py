# app/services/category_mapper.py
"""
Minimal keyword-based item categorizer for the budgeting feature
(app/routes/budgeting_routes.py). Matches are case-insensitive substrings
against the German grocery item names seen on Kaufland/REWE receipts.
Extend CATEGORY_KEYWORDS as new items show up uncategorized ("Other").
"""

CATEGORY_KEYWORDS = {
    "Dairy": [
        "milch", "h-milch", "vollmilch", "butter", "kaese", "käse", "joghurt",
        "quark", "sahne", "creme fraiche",
    ],
    "Bakery": [
        "brot", "broetchen", "brötchen", "toast", "kuchen", "croissant",
    ],
    "Produce": [
        "tomate", "rispentomate", "kartoffel", "zwiebel", "salat", "gurke",
        "paprika", "apfel", "banane", "karotte", "moehre", "möhre", "obst",
        "gemuese", "gemüse",
    ],
    "Meat & Fish": [
        "huhn", "hähnchen", "haehnchen", "rind", "schwein", "wurst", "schinken",
        "fisch", "lachs", "hackfleisch",
    ],
    "Pantry": [
        "mehl", "zucker", "nudel", "spaghetti", "reis", "haferflocken", "oel",
        "öl", "essig", "gewuerz", "gewürz", "salz", "pfeffer", "tortilla",
        "ei m-l", "ei l", "ei xl", "eier",
    ],
    "Beverages": [
        "wasser", "saft", "cola", "limo", "bier", "wein", "kaffee", "tee",
    ],
    "Frozen": [
        "tiefkuehl", "tiefkühl", "eis",
    ],
    "Snacks & Sweets": [
        "chips", "schokolade", "keks", "suessigkeit", "süssigkeit", "gummibaerchen",
    ],
    "Household": [
        "toilettenpapier", "klopapier", "kuechenrolle", "küchenrolle",
        "waschmittel", "spuelmittel", "spülmittel", "muellbeutel", "müllbeutel",
    ],
    "Personal Care": [
        "shampoo", "duschgel", "zahnpasta", "seife", "deo",
    ],
}

OTHER_CATEGORY = "Other"


def categorize_item(item_name: str) -> str:
    name = (item_name or "").lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in name for kw in keywords):
            return category
    return OTHER_CATEGORY


def all_categories() -> list[str]:
    return list(CATEGORY_KEYWORDS.keys()) + [OTHER_CATEGORY]
