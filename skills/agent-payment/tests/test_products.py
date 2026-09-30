import json
import unittest

from support import TOKEN, CommandTests, StubService, amount

import pws_client  # noqa: E402
from pws_client import ConfigurationError, ServiceError  # noqa: E402

UNPRINTABLE = "shirt\u0007"
NON_ASCII = "prod\u00ed"

PRODUCT = {
    "productId": "prod_1",
    "title": "Cotton shirt",
    "merchant": "Example Store",
    "price": amount("1500000"),
}
OTHER_PRODUCT = {
    "productId": "prod_2",
    "title": "Wool scarf",
    "price": amount("2500000"),
}
DETAIL = {
    "product": PRODUCT,
    "options": [{"name": "Size", "values": [
        {"optionId": "opt_s", "label": "S"},
        {"optionId": "opt_m", "label": "M"},
    ]}],
}
VARIANT = {
    "variantId": "var_1",
    "title": "Cotton shirt, M",
    "price": amount("1500000"),
    "purchasable": True,
}


class InputTests(unittest.TestCase):
    def test_identifier_passes_a_service_identifier_through(self):
        self.assertEqual(pws_client.normalize_identifier(" prod_1 "), "prod_1")

    def test_identifier_refuses_what_cannot_be_an_identifier(self):
        cases = ["", "   ", "prod 1", NON_ASCII, "a" * 201]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_identifier(value)

    def test_search_query_keeps_the_words_the_user_gave(self):
        self.assertEqual(
            pws_client.normalize_search_query("  blue cotton shirt "),
            "blue cotton shirt",
        )

    def test_search_query_refuses_empty_words(self):
        with self.assertRaises(ConfigurationError):
            pws_client.normalize_search_query("  ")

    def test_search_limit_stays_inside_one_screen(self):
        self.assertEqual(pws_client.normalize_search_limit(10), 10)
        self.assertEqual(pws_client.normalize_search_limit(" 10 "), 10)
        for value in (0, -1, 51, "", "many", "1.5", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_search_limit(value)

    def test_quantity_stays_a_uat_purchase(self):
        self.assertEqual(pws_client.normalize_quantity(2), 2)
        self.assertEqual(pws_client.normalize_quantity("2"), 2)
        for value in (0, 101, "two", ""):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_quantity(value)

    def test_option_carries_one_name_and_one_value(self):
        self.assertEqual(pws_client.normalize_option(" Size = M "), ("Size", "M"))

    def test_option_refuses_a_selection_without_a_value(self):
        for value in ("Size", "=M", "Size="):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    pws_client.normalize_option(value)


class ProductParseTests(unittest.TestCase):
    def test_a_search_reports_every_product_the_service_returns(self):
        products = pws_client.parse_products(
            {"products": [PRODUCT, OTHER_PRODUCT]}
        )
        self.assertEqual(
            [(product.product_id, product.title) for product in products],
            [("prod_1", "Cotton shirt"), ("prod_2", "Wool scarf")],
        )
        self.assertEqual(products[0].merchant, "Example Store")
        self.assertIsNone(products[1].merchant)
        self.assertEqual(str(products[0].price), "1.500000 USDC")
        self.assertEqual(str(products[1].price), "2.500000 USDC")

    def test_an_absent_list_is_an_empty_result(self):
        self.assertEqual(pws_client.parse_products({}), ())

    def test_a_product_without_a_price_reports_no_price(self):
        products = pws_client.parse_products(
            {"products": [{"productId": "prod_1", "title": "Cotton shirt"}]}
        )
        self.assertIsNone(products[0].price)
        self.assertIsNone(products[0].merchant)

    def test_a_search_refuses_a_body_it_cannot_report(self):
        cases = {
            "not an object": [],
            "products not a list": {"products": {}},
            "entry not an object": {"products": ["prod_1"]},
            "no product id": {"products": [{"title": "Cotton shirt"}]},
            "no title": {"products": [{"productId": "prod_1"}]},
            "unprintable title": {
                "products": [{"productId": "prod_1", "title": UNPRINTABLE}]
            },
            "too many products": {"products": [PRODUCT] * 51},
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_products(body)

    def test_a_detail_reports_the_options_that_decide_the_variant(self):
        detail = pws_client.parse_product_detail(DETAIL)
        self.assertEqual(detail.product.product_id, "prod_1")
        self.assertEqual(detail.options[0].name, "Size")
        self.assertEqual(
            [(value.option_id, value.label) for value in detail.options[0].values],
            [("opt_s", "S"), ("opt_m", "M")],
        )

    def test_a_detail_without_options_reports_no_options(self):
        detail = pws_client.parse_product_detail({"product": PRODUCT})
        self.assertEqual(detail.options, ())

    def test_a_detail_refuses_an_option_it_cannot_offer(self):
        cases = {
            "no product": {"options": []},
            "option without values": {
                "product": PRODUCT,
                "options": [{"name": "Size"}],
            },
            "option without a name": {
                "product": PRODUCT,
                "options": [{"values": ["S"]}],
            },
            "option id missing": {
                "product": PRODUCT,
                "options": [{"name": "Size", "values": [{"label": "S"}]}],
            },
            "option label missing": {
                "product": PRODUCT,
                "options": [{"name": "Size", "values": [{"optionId": "opt_s"}]}],
            },
            "option value not an object": {
                "product": PRODUCT,
                "options": [{"name": "Size", "values": [1]}],
            },
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_product_detail(body)

    def test_a_variant_reports_the_price_the_quote_will_use(self):
        variant = pws_client.parse_variant(VARIANT)
        self.assertEqual(variant.variant_id, "var_1")
        self.assertTrue(variant.purchasable)
        self.assertEqual(str(variant.price), "1.500000 USDC")

    def test_an_absent_flag_reports_a_variant_that_cannot_be_bought(self):
        variant = pws_client.parse_variant({"variantId": "var_1"})
        self.assertFalse(variant.purchasable)
        self.assertIsNone(variant.price)
        self.assertIsNone(variant.title)

    def test_a_variant_refuses_a_body_it_cannot_report(self):
        cases = {
            "no variant id": {"purchasable": True},
            "purchasable without a price": {"variantId": "var_1", "purchasable": True},
            "purchasable at no price": {
                "variantId": "var_1",
                "purchasable": True,
                "price": amount("0"),
            },
            "flag not a flag": {"variantId": "var_1", "purchasable": "yes"},
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ServiceError):
                    pws_client.parse_variant(body)


class ProductServiceTests(unittest.TestCase):
    def test_a_search_asks_the_service_for_the_words_and_the_limit(self):
        with StubService(responses=({"products": [PRODUCT]},)) as service:
            products = pws_client.search_products(
                service.url, TOKEN, "cotton shirt", limit=5
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.PRODUCTS_PATH}?query=cotton+shirt&limit=5")],
            )
            self.assertEqual(service.authorizations, [f"Bearer {TOKEN}"])
        self.assertEqual(len(products), 1)

    def test_a_search_without_a_limit_leaves_the_limit_to_the_service(self):
        with StubService(responses=({},)) as service:
            self.assertEqual(
                pws_client.search_products(service.url, TOKEN, "shirt"), ()
            )
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.PRODUCTS_PATH}?query=shirt")]
            )

    def test_a_detail_read_escapes_the_identifier_in_the_path(self):
        with StubService(responses=(DETAIL,)) as service:
            detail = pws_client.read_product(service.url, TOKEN, "prod/1")
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.PRODUCTS_PATH}/prod%2F1")]
            )
        self.assertEqual(detail.product.product_id, "prod_1")

    def test_a_variant_resolution_sends_the_selection_to_the_service(self):
        with StubService(responses=(DETAIL, VARIANT)) as service:
            variant = pws_client.resolve_variant(
                service.url, TOKEN, "prod_1", (("Size", "M"),)
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.PRODUCTS_PATH}/prod_1"),
                 ("POST", f"{pws_client.PRODUCTS_PATH}/prod_1/variant")],
            )
            self.assertEqual(
                json.loads(service.bodies[1]),
                {"optionIds": ["opt_m"]},
            )
        self.assertEqual(variant.variant_id, "var_1")

    def test_a_variant_without_options_sends_an_empty_id_list(self):
        with StubService(responses=(VARIANT,)) as service:
            pws_client.resolve_variant(service.url, TOKEN, "prod_1", ())
            self.assertEqual(json.loads(service.bodies[0]), {"optionIds": []})
            self.assertEqual(len(service.calls), 1)

    def test_unknown_or_repeated_choices_never_reach_variant_resolution(self):
        for selection in (
            (("Size", "XL"),),
            (("Color", "Blue"),),
            (("Size", "S"), ("Size", "M")),
        ):
            with self.subTest(selection=selection):
                with StubService(responses=(DETAIL,)) as service:
                    with self.assertRaises(ConfigurationError):
                        pws_client.resolve_variant(service.url, TOKEN, "prod_1", selection)
                    self.assertEqual(service.calls, [("GET", f"{pws_client.PRODUCTS_PATH}/prod_1")])

    def test_ambiguous_labels_never_choose_an_arbitrary_id(self):
        ambiguous = DETAIL | {"options": [{"name": "Size", "values": [
            {"optionId": "opt_m1", "label": "M"},
            {"optionId": "opt_m2", "label": "M"},
        ]}]}
        with StubService(responses=(ambiguous,)) as service:
            with self.assertRaises(ConfigurationError):
                pws_client.resolve_variant(service.url, TOKEN, "prod_1", (("Size", "M"),))
            self.assertEqual(len(service.calls), 1)

    def test_a_response_that_repeats_a_quoted_token_is_refused(self):
        token = 'head"er.body.signature'
        body = {"products": [PRODUCT | {"title": f"seen {token}"}]}
        with StubService(responses=(body,)) as service:
            with self.assertRaises(ServiceError):
                pws_client.search_products(service.url, token, "shirt")

    def test_a_response_that_repeats_the_session_token_is_refused(self):
        body = {"products": [PRODUCT | {"title": TOKEN}]}
        with StubService(responses=(body,)) as service:
            with self.assertRaises(ServiceError):
                pws_client.search_products(service.url, TOKEN, "shirt")


class ProductCommandTests(CommandTests):
    def test_the_products_command_reports_one_row_for_every_product(self):
        body = {"products": [PRODUCT, OTHER_PRODUCT]}
        with StubService(responses=(body,)) as service:
            code, out, err = self.run_command(
                "products", service.url, "--query", "cotton shirt", "--limit", "5"
            )
            self.assertEqual(
                service.calls,
                [("GET", f"{pws_client.PRODUCTS_PATH}?query=cotton+shirt&limit=5")],
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("prod_1 | Cotton shirt | Example Store | 1.500000 USDC", out)
        self.assertIn("prod_2 | Wool scarf | 2.500000 USDC", out)
        self.assertIn("Next: read one product", out)

    def test_the_products_command_reports_a_product_without_a_price(self):
        body = {"products": [{"productId": "prod_1", "title": "Cotton shirt"}]}
        with StubService(responses=(body,)) as service:
            code, out, _ = self.run_command("products", service.url, "--query", "shirt")
        self.assertEqual(code, 0)
        self.assertIn("prod_1 | Cotton shirt | no price", out)

    def test_an_empty_search_tells_the_agent_to_retry_before_asking(self):
        with StubService(responses=({},)) as service:
            code, out, _ = self.run_command("products", service.url, "--query", "shirt")
            self.assertEqual(len(service.calls), 1)
        self.assertEqual(code, 0)
        self.assertIn("No products found.", out)
        self.assertIn(
            "Next: run the same search again, up to 2 more times. "
            "Then search with only the product name, without a brand or store name. "
            "If the search still finds nothing, ask the user for more detail.",
            out,
        )

    def test_the_products_command_refuses_a_limit_it_cannot_print(self):
        with StubService(responses=({},)) as service:
            code, _, err = self.run_command(
                "products", service.url, "--query", "shirt", "--limit", "51"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Search limit must be between 1 and 50.", err)
        self.assertIn("action: Correct the command input", err)

    def test_the_products_command_refuses_a_limit_that_is_not_a_number(self):
        with StubService(responses=({},)) as service:
            code, _, err = self.run_command(
                "products", service.url, "--query", "shirt", "--limit", "many"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Search limit must be a whole number.", err)
        self.assertIn("action: Correct the command input", err)

    def test_the_product_command_reports_the_options_to_select(self):
        with StubService(responses=(DETAIL,)) as service:
            code, out, err = self.run_command("product", service.url, "prod_1")
            self.assertEqual(
                service.calls, [("GET", f"{pws_client.PRODUCTS_PATH}/prod_1")]
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Option Size: S, M", out)
        self.assertIn("Next: resolve the selection with the variant command", out)

    def test_the_variant_command_sends_every_selection(self):
        with StubService(responses=(DETAIL, VARIANT)) as service:
            code, out, err = self.run_command(
                "variant", service.url, "prod_1", "--option", "Size=M"
            )
            self.assertEqual(
                json.loads(service.bodies[1]),
                {"optionIds": ["opt_m"]},
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Variant id: var_1", out)
        self.assertIn("Price: 1.500000 USDC", out)
        self.assertIn("Variant: purchasable", out)

    def test_a_variant_that_cannot_be_bought_stops_before_a_quote(self):
        with StubService(responses=(DETAIL, {"variantId": "var_1"})) as service:
            code, out, _ = self.run_command(
                "variant", service.url, "prod_1", "--option", "Size=M"
            )
        self.assertEqual(code, 0)
        self.assertIn("Variant: not purchasable", out)
        self.assertIn("Do not create a quote.", out)

    def test_the_variant_command_refuses_a_selection_without_a_value(self):
        with StubService(responses=(VARIANT,)) as service:
            code, _, err = self.run_command(
                "variant", service.url, "prod_1", "--option", "Size"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Option must be stated as name=value.", err)
        self.assertIn("action: Correct the command input", err)

    def test_the_product_command_refuses_an_identifier_it_cannot_send(self):
        with StubService(responses=(DETAIL,)) as service:
            code, _, err = self.run_command("product", service.url, "prod 1")
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Identifier must be ASCII text without spaces.", err)
        self.assertIn("action: Correct the command input", err)

    def test_the_products_command_refuses_words_it_cannot_send(self):
        with StubService(responses=({},)) as service:
            code, _, err = self.run_command(
                "products", service.url, "--query", "\u0007"
            )
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Search query must be printable text.", err)
        self.assertIn("action: Correct the command input", err)

    def test_the_products_command_asks_for_the_search_words_it_needs(self):
        with StubService(responses=({},)) as service:
            code, _, err = self.run_command("products", service.url)
            self.assertEqual(service.calls, [])
        self.assertEqual(code, 1)
        self.assertIn("Command input is not valid", err)
        self.assertIn("action: Correct the command input", err)

    def test_a_search_the_helper_cannot_report_asks_for_operator_help(self):
        body = {"products": [{"title": "Cotton shirt"}]}
        with StubService(responses=(body,)) as service:
            code, _, err = self.run_command("products", service.url, "--query", "s")
        self.assertEqual(code, 1)
        self.assertIn("Product search response field productId is not text.", err)
        self.assertIn("action: Report the service error", err)

    def test_a_product_the_helper_cannot_report_asks_for_operator_help(self):
        body = {"product": PRODUCT, "options": [{"name": "Size", "values": []}]}
        with StubService(responses=(body,)) as service:
            code, _, err = self.run_command("product", service.url, "prod_1")
        self.assertEqual(code, 1)
        self.assertIn("Product response reports an option without values.", err)
        self.assertIn("action: Report the service error", err)

    def test_a_variant_the_helper_cannot_report_asks_for_operator_help(self):
        body = {"variantId": "var_1", "purchasable": True}
        with StubService(responses=(DETAIL, body)) as service:
            code, _, err = self.run_command(
                "variant", service.url, "prod_1", "--option", "Size=M"
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "Variant response reports a purchasable variant without a price.", err
        )
        self.assertIn("action: Report the service error", err)


if __name__ == "__main__":
    unittest.main()
