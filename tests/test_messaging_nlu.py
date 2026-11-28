"""The deterministic messaging grammar — no model, no network.

These are the sentences a person actually speaks on both surfaces; the test
table is the contract the desktop and phone planners share.
"""

from __future__ import annotations

import unittest

from novacontrol.intelligence.messaging import parse_message_request


class MessagingGrammarTests(unittest.TestCase):
    def test_named_app_with_a_recipient_and_body(self) -> None:
        request = parse_message_request("send a hi msg to you in whatsapp")
        self.assertEqual((request.app, request.recipient, request.body), ("whatsapp", "you", "hi"))

    def test_app_preposition_and_saying(self) -> None:
        request = parse_message_request("msg you in whatsapp saying hi")
        self.assertEqual((request.app, request.recipient, request.body), ("whatsapp", "you", "hi"))

    def test_mail_subject_and_address(self) -> None:
        request = parse_message_request(
            "mail hi using subject testing to vallabhaneninarasimha134@gmail.com"
        )
        self.assertEqual(request.app, "mail")
        self.assertEqual(request.recipient, "vallabhaneninarasimha134@gmail.com")
        self.assertEqual(request.subject, "testing")
        self.assertEqual(request.body, "hi")

    def test_chain_fragment_keeps_the_recipient_and_body(self) -> None:
        request = parse_message_request("compose an email to bob saying hello")
        self.assertEqual((request.recipient, request.body), ("bob", "hello"))

    def test_quoted_operands(self) -> None:
        request = parse_message_request("text 'mom' 'running late'")
        self.assertEqual((request.recipient, request.body), ("mom", "running late"))

    def test_named_app_and_saying(self) -> None:
        request = parse_message_request("text mom on telegram saying running late")
        self.assertEqual((request.app, request.recipient, request.body), ("telegram", "mom", "running late"))

    def test_a_verb_without_a_message_is_not_a_request(self) -> None:
        self.assertIsNone(parse_message_request("open whatsapp"))

    def test_default_app_applies_only_when_none_is_named(self) -> None:
        request = parse_message_request("text mom saying hi", default_app="gmail")
        self.assertEqual(request.app, "gmail")
        request = parse_message_request("text mom on whatsapp saying hi", default_app="gmail")
        self.assertEqual(request.app, "whatsapp")


if __name__ == "__main__":
    unittest.main()
