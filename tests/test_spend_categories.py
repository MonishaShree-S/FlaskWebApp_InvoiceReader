import unittest

from app.services.spend_categories import Rule, RuleMatcher, normalize, suggest_keyword
from app.services.spend_category_seed import CATEGORIES, GLOBAL_RULES, UNCATEGORIZED_SLUG


def global_matcher(extra_user_rules=()):
    slugs = [c[0] for c in CATEGORIES]
    rules = [Rule(i, normalize(kw), slugs.index(slug), False) for i, (kw, slug) in enumerate(GLOBAL_RULES.items())]
    rules += [Rule(10_000 + i, normalize(kw), slugs.index(slug), True) for i, (kw, slug) in enumerate(extra_user_rules)]
    return RuleMatcher(rules), slugs


def categorize(matcher, slugs, text):
    rule = matcher.match(text)
    return slugs[rule.category_id] if rule else None


class NormalizeTest(unittest.TestCase):
    def test_umlauts_and_case(self):
        self.assertEqual(normalize("MÄC Geiz  Straße"), "maec geiz strasse")

    def test_broken_encoding_is_dropped(self):
        self.assertEqual(normalize("Geb�hr Rechnungsr�ckgabe"), "gebhr rechnungsrckgabe")


class SeedIntegrityTest(unittest.TestCase):
    def test_slugs_unique_and_parents_exist(self):
        slugs = [c[0] for c in CATEGORIES]
        self.assertEqual(len(slugs), len(set(slugs)))
        for slug, _, parent, _ in CATEGORIES:
            if parent:
                self.assertIn(parent, slugs, slug)
                self.assertTrue(slug.startswith(parent + "."), slug)
        self.assertIn(UNCATEGORIZED_SLUG, slugs)

    def test_rules_point_to_existing_categories(self):
        slugs = {c[0] for c in CATEGORIES}
        for kw, slug in GLOBAL_RULES.items():
            self.assertIn(slug, slugs, kw)

    def test_rule_keywords_already_normalized(self):
        for kw in GLOBAL_RULES:
            self.assertEqual(kw, normalize(kw))


class RuleMatcherTest(unittest.TestCase):
    def setUp(self):
        self.matcher, self.slugs = global_matcher()

    def assertCategory(self, text, slug):
        self.assertEqual(categorize(self.matcher, self.slugs, text), slug, text)

    def test_examples_from_requirements(self):
        self.assertCategory("BURGER KING 8577 SOT", "dining.fast_food")
        for text in ("dm-drogerie markt GmbH", "DM DROGERIE SAGT DANKE", "DM PM 187"):
            self.assertCategory(text, "household.drugstores")
        for text in ("Woolworth GmbH Fil. 12", "Kik Fil 8192", "TEDI Fil. 6400"):
            self.assertCategory(text, "household.variety")
        self.assertCategory("JYSK BERLIN-EICHE AHRE", "household.home")
        self.assertCategory("APOTHEKE HELLE MITTE", "health.pharmacy")
        for text in ("DEICHMANN Berlin 1232", "Foot Locker Germany Gm", "C&A Haus 2222",
                     "PRIMARK BERLIN ALEXAND", "TK Maxx", "H&M DE0334", "PAYPAL HMHENNESMAU"):
            self.assertCategory(text, "shopping.clothing")
        self.assertCategory("APPLE GERMANY R443", "shopping.electronics")
        self.assertCategory("Lovisa", "shopping.accessories")
        self.assertCategory("TASCHENPARADIES", "shopping.accessories")
        self.assertCategory("RYANAIR 224YU9U5S", "travel.flights")
        self.assertCategory("NORWEGIAN AIR SHUTTLE", "travel.flights")

    def test_groceries(self):
        for text in ("REWE Ivonne Schoen", "KAUFLAND EICHE KAUFPAR", "Lidl sagt Danke", "ALDI NORD", "kaufland"):
            self.assertCategory(text, "groceries.supermarkets")
        for text in ("SumUp WATAN Internati", "India-Go Market", "goindia", "afghan shop", "Thang Long AsiaFood"):
            self.assertCategory(text, "groceries.international")

    def test_longest_keyword_wins(self):
        self.assertCategory("Lekkerland REWE To Go", "groceries.convenience")
        self.assertCategory("AMAZON PRIM NZ9VY1YP4", "subscriptions")
        self.assertCategory("AMAZON NU9LS6WE4", "shopping.online")

    def test_short_keywords_need_whole_word(self):
        self.assertIsNone(self.matcher.match("ADMIRAL CASINO"))   # contains "dm"
        self.assertIsNone(self.matcher.match("KIKKOMAN STORE"))   # starts with "kik"
        self.assertCategory("SumUp Mierenback Caf", "dining.cafes")
        self.assertCategory("ZRACNA LUKA ZADAR CAFF", "dining.cafes")

    def test_keyword_must_start_at_word_boundary(self):
        self.assertIsNone(self.matcher.match("XREWE"))

    def test_mojibake_fee_row(self):
        self.assertCategory("Geb�hr Rechnungsr�ckgabe", "fees")

    def test_card_top_up_is_transfer(self):
        self.assertCategory("EINZAHLUNG AUF KARTE **8193", "transfers")

    def test_no_match(self):
        self.assertIsNone(self.matcher.match("STONE GARDEN"))
        self.assertIsNone(self.matcher.match(""))
        self.assertIsNone(self.matcher.match(None))

    def test_user_rule_beats_longer_global_rule(self):
        matcher, slugs = global_matcher(extra_user_rules=[("kik", "shopping.clothing")])
        self.assertEqual(categorize(matcher, slugs, "Kik Fil 8192"), "shopping.clothing")


class SuggestKeywordTest(unittest.TestCase):
    def test_skips_payment_provider_prefixes(self):
        self.assertEqual(suggest_keyword("SumUp Nepal Momo"), "nepal")
        self.assertEqual(suggest_keyword("PAYPAL MEDIAMARKT"), "mediamarkt")

    def test_first_word(self):
        self.assertEqual(suggest_keyword("Kik Fil 8192"), "kik")
        self.assertEqual(suggest_keyword("H&M DE0334"), "h&m")


if __name__ == "__main__":
    unittest.main()
