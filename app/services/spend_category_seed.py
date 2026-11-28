"""
Seed data for purchase (spend) categories and the global merchant rules.

This is *purchase-level* categorization (what a card charge / receipt was
for: Groceries, Dining Out, Travel...). It is unrelated to the *item-level*
categories in groceries.category (Dairy, Bakery...) used by budgeting.

Loaded into SpendCategories / CategoryRules by
spend_categories.ensure_schema() at startup. Seeding only inserts rows that
are missing (matched by slug / keyword), so edits made in the database are
never overwritten. To change an existing global rule, update the DB row.

Keywords are matched against the card description or shop name after
normalization (lowercase, umlauts -> ae/oe/ue, ß -> ss). A keyword must
start at a word boundary; keywords of 3 characters or fewer must also end at
one. The longest matching keyword wins; a user's own rules beat global ones.
"""

# (slug, name, parent_slug, counts_as_spending)
# Two levels: purchases are assigned to a leaf or a top-level category, and
# reports roll up to the top level.
CATEGORIES = [
    ("groceries", "Groceries", None, True),
    ("groceries.supermarkets", "Supermarkets", "groceries", True),
    ("groceries.international", "International & Ethnic Stores", "groceries", True),
    ("groceries.convenience", "Kiosks & Convenience", "groceries", True),

    ("dining", "Dining Out", None, True),
    ("dining.restaurants", "Restaurants", "dining", True),
    ("dining.fast_food", "Fast Food", "dining", True),
    ("dining.cafes", "Cafés & Bakeries", "dining", True),
    ("dining.delivery", "Food Delivery", "dining", True),

    ("household", "Household", None, True),
    ("household.drugstores", "Drugstores & Personal Care", "household", True),
    ("household.home", "Home & Furnishing", "household", True),
    ("household.variety", "Discount & Variety Stores", "household", True),

    ("health", "Health", None, True),
    ("health.pharmacy", "Pharmacy", "health", True),
    ("health.medical", "Doctors & Medical", "health", True),

    ("shopping", "Shopping", None, True),
    ("shopping.clothing", "Clothing & Shoes", "shopping", True),
    ("shopping.accessories", "Accessories & Gifts", "shopping", True),
    ("shopping.electronics", "Electronics", "shopping", True),
    ("shopping.online", "Online Marketplaces", "shopping", True),

    ("travel", "Travel", None, True),
    ("travel.flights", "Flights", "travel", True),
    ("travel.ground", "Trains, Buses & Transfers", "travel", True),
    ("travel.accommodation", "Accommodation", "travel", True),

    ("transport", "Local Transport", None, True),
    ("transport.public", "Public Transport", "transport", True),
    ("transport.taxi", "Taxi & Ride-hailing", "transport", True),
    ("transport.sharing", "Bike & Scooter Sharing", "transport", True),

    ("entertainment", "Entertainment", None, True),
    ("subscriptions", "Subscriptions", None, True),
    ("education", "Education", None, True),
    ("fees", "Fees & Charges", None, True),

    # Money moved, not spent: excluded from spending totals.
    ("transfers", "Transfers & Card Payments", None, False),

    ("uncategorized", "Uncategorized", None, True),
]

UNCATEGORIZED_SLUG = "uncategorized"

# keyword -> category slug
GLOBAL_RULES = {
    # Groceries
    "rewe": "groceries.supermarkets",
    "kaufland": "groceries.supermarkets",
    "aldi": "groceries.supermarkets",
    "lidl": "groceries.supermarkets",
    "netto": "groceries.supermarkets",
    "penny": "groceries.supermarkets",
    "edeka": "groceries.supermarkets",
    "konzum": "groceries.supermarkets",
    "ribola": "groceries.supermarkets",
    "india-go": "groceries.international",
    "goindia": "groceries.international",
    "indiansupermarkt": "groceries.international",
    "watan": "groceries.international",
    "afghan": "groceries.international",
    "asiafood": "groceries.international",
    "go asia": "groceries.international",
    "asia might": "groceries.international",
    "baraka": "groceries.international",
    "rewe to go": "groceries.convenience",
    "tisak": "groceries.convenience",
    "theo automaten": "groceries.convenience",

    # Dining out
    "burger king": "dining.fast_food",
    "mcdonalds": "dining.fast_food",
    "dominos": "dining.fast_food",
    "kfc": "dining.fast_food",
    "subway": "dining.fast_food",
    "fast food": "dining.fast_food",
    "nepal momo": "dining.restaurants",
    "restaurant": "dining.restaurants",
    "cafe": "dining.cafes",
    "caffe": "dining.cafes",
    "caff": "dining.cafes",  # card descriptions are cut at 22 characters
    "caf": "dining.cafes",
    "kavana": "dining.cafes",
    "dat cookie": "dining.cafes",
    "mlinar": "dining.cafes",
    "baeckerei": "dining.cafes",
    "lieferando": "dining.delivery",
    "wolt": "dining.delivery",
    "uber eats": "dining.delivery",

    # Household
    "dm": "household.drugstores",
    "dm-drogerie": "household.drugstores",
    "rossman": "household.drugstores",
    "mueller": "household.drugstores",
    "jysk": "household.home",
    "ikea": "household.home",
    "obi": "household.home",
    "hornbach": "household.home",
    "bauhaus": "household.home",
    "tedi": "household.variety",
    "woolworth": "household.variety",
    "kik": "household.variety",
    "action": "household.variety",
    "maec geiz": "household.variety",
    "euroshop": "household.variety",

    # Health
    "apotheke": "health.pharmacy",
    "pharmacy": "health.pharmacy",
    "ljekarna": "health.pharmacy",

    # Shopping
    "deichmann": "shopping.clothing",
    "foot locker": "shopping.clothing",
    "c&a": "shopping.clothing",
    "primark": "shopping.clothing",
    "tk maxx": "shopping.clothing",
    "tkmaxx": "shopping.clothing",
    "h&m": "shopping.clothing",
    "hmhennes": "shopping.clothing",
    "shein": "shopping.clothing",
    "zalando": "shopping.clothing",
    "lovisa": "shopping.accessories",
    "taschenparadies": "shopping.accessories",
    "suveniri": "shopping.accessories",
    "apple": "shopping.electronics",
    "mediamarkt": "shopping.electronics",
    "saturn": "shopping.electronics",
    "amazon": "shopping.online",
    "amzn": "shopping.online",
    "pay.amazon": "shopping.online",
    "www.amazon": "shopping.online",
    "ebay": "shopping.online",

    # Travel
    "ryanair": "travel.flights",
    "norwegian": "travel.flights",
    "easyjet": "travel.flights",
    "eurowings": "travel.flights",
    "lufthansa": "travel.flights",
    "wizz": "travel.flights",
    "flix": "travel.ground",
    "dbvertrieb": "travel.ground",
    "deutsche bahn": "travel.ground",
    "tripcom": "travel.ground",
    "anttripcom": "travel.ground",
    "liburnija": "travel.ground",
    "terravision": "travel.ground",
    "hostelworld": "travel.accommodation",
    "www.hostelworld": "travel.accommodation",
    "booking.com": "travel.accommodation",
    "airbnb": "travel.accommodation",
    "camp": "travel.accommodation",

    # Local transport
    "bvg": "transport.public",
    "bolt": "transport.taxi",
    "uber": "transport.taxi",
    "nextbike": "transport.sharing",
    "lime": "transport.sharing",

    # Entertainment / subscriptions / education / fees / transfers
    "uci": "entertainment",
    "cinemaxx": "entertainment",
    "amazon prim": "subscriptions",
    "netflix": "subscriptions",
    "spotify": "subscriptions",
    "goethe": "education",
    "rechnungsr": "fees",
    "gebuehr": "fees",
    "einzahlung": "transfers",
}
