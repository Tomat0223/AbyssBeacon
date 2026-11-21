import io
import json
import unittest
from email.message import Message
from unittest.mock import patch

from . import renderoverse
from .source_contract import missing_model_fields


def row(**changes):
    return {
        "type": "lora", "slug": "example-model", "name": "Example Model",
        "baseModel": "Illustrious", "authorName": "Example Author", "nsfw": False,
        "directPriceCents": 0, "hasDirectDownload": True, **changes,
    }


def payload(*rows):
    return {"models": list(rows), "total": len(rows)}


class RenderoverseTests(unittest.TestCase):
    def test_source_contract_and_open_page_only(self):
        [model] = renderoverse.normalize_catalog(payload(row(
            coverUrl="https://example.invalid/cover", fileUrl="https://example.invalid/file",
            authorId="not-copied", license="not-inferred",
        )))
        self.assertEqual(missing_model_fields(model), [])
        self.assertEqual(model.url, "https://renderoverse.com/model/example-model")
        self.assertEqual(model.model_type, "LoRA")
        self.assertEqual(model.architecture, "Illustrious")
        self.assertEqual(model.author, "Example Author")
        self.assertTrue(model.gated)
        self.assertFalse(model.sensitive)
        self.assertEqual((model.files, model.media, model.image, model.license), ([], [], "", ""))
        self.assertNotIn("example.invalid", json.dumps(model.as_dict()))
        self.assertNotIn("not-copied", json.dumps(model.as_dict()))

    def test_maturity_must_be_explicitly_false(self):
        for maturity in (True, None, 0, "false"):
            with self.subTest(maturity=maturity):
                self.assertEqual(renderoverse.normalize_catalog(payload(row(nsfw=maturity))), [])

    def test_ignores_duplicates_bad_slugs_and_non_loras(self):
        rows = [row(), row(), row(type="checkpoint")]
        for slug in ("../x", "some%2Fpath", "x?y=z", "https://elsewhere.invalid", "two words"):
            rows.append(row(slug=slug))
        self.assertEqual(len(renderoverse.normalize_catalog(payload(*rows))), 1)

    def test_price_is_exact_or_unknown(self):
        for changes, expected in (
            ({}, 0), ({"directPriceCents": 500, "priceCents": 0}, 500),
            ({"directPriceCents": False}, None), ({"directPriceCents": -1}, None),
            ({"hasDirectDownload": False}, None), ({"directPriceCents": None}, None),
        ):
            with self.subTest(changes=changes):
                [model] = renderoverse.normalize_catalog(payload(row(**changes)))
                self.assertEqual(model.card_data["renderoverse"]["direct_price_cents"], expected)

    def test_local_text_and_exact_base_model(self):
        with patch.object(renderoverse, "fetch_catalog", return_value=payload(row())) as fetch:
            self.assertEqual(len(renderoverse.scan(" example ")), 1)
            self.assertEqual(len(renderoverse.scan(" illusTRIOUS ", scan_settings={"_search_mode": "base_model"})), 1)
            self.assertEqual(renderoverse.scan("example", scan_settings={"_search_mode": "base_model"}), [])
            self.assertEqual(renderoverse.scan("unused", scan_settings={"_search_mode": "filter"}), [])
            self.assertEqual(fetch.call_count, 3)
            fetch.assert_called_with()

    def test_catalog_shape(self):
        for item in ([], {}, {"models": [], "total": True}, {"models": [row()], "total": 0}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                renderoverse.normalize_catalog(item)

    def test_fetch_only_uses_fixed_credential_free_catalog_url(self):
        with patch.object(renderoverse, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.headers = Message()
            response.headers["Content-Type"] = "application/json"
            response.read.side_effect = io.BytesIO(b'{"models": [], "total": 0}').read
            self.assertEqual(renderoverse.fetch_catalog(), payload())
            req, = opener.return_value.open.call_args.args
            self.assertEqual(req.full_url, "https://renderoverse.com/api/models?type=lora")
            self.assertEqual(req.get_method(), "GET")
            self.assertFalse(req.has_header("Authorization"))
            self.assertFalse(req.has_header("Cookie"))
            self.assertIs(opener.call_args.args[0], renderoverse.NoRedirects)
            response.read.assert_called_once_with(renderoverse.MAX_BYTES + 1)
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 10)

    def test_rejects_non_json_and_large_responses(self):
        with patch.object(renderoverse, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.headers = Message()
            response.headers["Content-Type"] = "text/html"
            with self.assertRaisesRegex(ValueError, "Expected"):
                renderoverse.fetch_catalog()
            response.read.assert_not_called()
            response.headers.replace_header("Content-Type", "application/json")
            response.read.return_value = b" " * (renderoverse.MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "size limit"):
                renderoverse.fetch_catalog()


if __name__ == "__main__":
    unittest.main()
