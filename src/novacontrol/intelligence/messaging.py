"""Deterministic messaging language — one grammar, no model, no network.

Every messaging surface reads the same sentence the same way here: the desktop
(WhatsApp, Gmail, Telegram, …) and the paired phone (SMS/WhatsApp). Four things
are extracted:

* **app** — which surface to draft in. Answered by a declared table plus either
  a leading app name ("whatsapp, text mom …") or a preposition
  ("… on whatsapp", "… using gmail");
* **recipient** — after "to"/"for", or the name before "saying";
* **subject** — after "subject" (optionally "using/with … subject");
* **body** — after "saying"/"message:"/":", or the verb's own object
  ("send a hi msg to mom").

Nothing here calls a language model, the network or a clock. The rules are
ordered so an explicit marker always beats a guess, and a sentence the rules
cannot read returns ``None`` — callers refuse instead of inventing a recipient.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re

# Declared surfaces. Data, not branches: adding an app is one row here.
MESSAGE_APPS: tuple[str, ...] = (
    "whatsapp",
    "telegram",
    "signal",
    "messenger",
    "instagram",
    "discord",
    "slack",
    "teams",
    "sms",
    "messages",
    "text",
    "gmail",
    "mail",
    "email",
    "outlook",
)

# Spoken aliases normalised onto the canonical surface name.
_APP_ALIASES: dict[str, str] = {
    "email": "mail",
    "e-mail": "mail",
    "text": "sms",
    "messages": "sms",
}

_APP_ALTERNATION = "|".join(sorted({*MESSAGE_APPS, *_APP_ALIASES}, key=len, reverse=True))
_QUOTED = r"'[^']+'|\"[^\"]+\""
_EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
_PHONE = r"\+?\d[\d\s().-]{5,}\d"
_SEPARATORS = r"saying|says|that\s+says|with\s+the\s+message|with\s+message|message|body|:"

_APP_HINT = re.compile(rf"\b(?:on|via|through|in|using)\s+(?P<app>{_APP_ALTERNATION})\b", re.IGNORECASE)
_APP_LEAD = re.compile(rf"^(?:hey\s+|ok\s+|please\s+)?(?P<app>{_APP_ALTERNATION})\b[\s,:]+", re.IGNORECASE)
_VERB_LEAD = re.compile(
    r"^(?:please\s+)?(?:send|compose|draft|write|text|message|msg|dm|mail|email)\s+"
    r"(?:(?:a|an|the)\s+)?(?:(?:text|message|msg|mail|email|dm)\s+(?:to\s+)?)?",
    re.IGNORECASE,
)
_SUBJECT = re.compile(
    rf"\b(?:using|with|having|about)?\s*subject\s*(?:is|:|=)?\s*(?P<subject>{_QUOTED}|.+?)"
    rf"(?=\s+(?:to|and|body|message)\b|$)",
    re.IGNORECASE,
)
_TO_MARKER = re.compile(
    rf"\b(?:to|for)\s+(?P<recipient>{_QUOTED}|{_EMAIL}|{_PHONE}|.+?)"
    rf"(?=\s+(?:{_SEPARATORS}|using|subject)\b|$)",
    re.IGNORECASE,
)
_SAY_MARKER = re.compile(rf"\b(?:{_SEPARATORS})\s*(?P<body>.+)$", re.IGNORECASE)
_NAME_SAYING = re.compile(rf"^(?P<name>{_QUOTED}|.+?)\s+(?:{_SEPARATORS})\s*(?P<body>.+)$", re.IGNORECASE)
_PLAIN_PAIR = re.compile(
    rf"^(?P<recipient>{_QUOTED}|\S+)\s+(?P<body>.+)$", re.IGNORECASE
)
_TRAILING_NOUN = re.compile(r"\b(?:msg|message|text|mail|email|dm)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MessageRequest:
    """What a messaging sentence asks for, in the shape every surface needs."""

    app: str = ""
    recipient: str = ""
    subject: str = ""
    body: str = ""


def _clean(value: str) -> str:
    return " ".join(re.sub(r"^[\s.,!?;:]+|[\s.,!?;:]+$", "", value.strip()).split())


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _canonical_app(value: str) -> str:
    lowered = value.strip().lower()
    return _APP_ALIASES.get(lowered, lowered)


def _strip_edges(value: str) -> str:
    return value.strip().strip(" ,.;:!?").strip()


def parse_message_request(
    raw: str, *, default_app: str = "", known_contacts: Iterable[str] = ()
) -> MessageRequest | None:
    """Read one messaging sentence, or return ``None`` when it is not one.

    ``default_app`` is the app the caller already knows (a chat that was just
    opened); an app named in the sentence always wins over it.
    """
    text = _clean(raw)
    if not text:
        return None

    # 1. The verb. A sentence with no messaging verb and no app is not ours.
    verb = _VERB_LEAD.match(text)
    verb_app = ""
    if verb:
        word = text[: verb.end()].strip().lower()
        if word.startswith(("mail", "email")):
            verb_app = "mail"
        text = text[verb.end():].strip()
    elif not re.match(rf"^(?:{_APP_ALTERNATION})\b", text, re.IGNORECASE) and not _APP_HINT.search(text):
        return None

    app = ""
    hint = _APP_HINT.search(text)
    if hint:
        app = _canonical_app(hint.group("app"))
        text = f"{text[: hint.start()]} {text[hint.end():]}".strip()
    else:
        lead = _APP_LEAD.match(text)
        if lead:
            app = _canonical_app(lead.group("app"))
            text = text[lead.end():].strip()
    app = app or verb_app or default_app

    # 2. Subject, before the recipient so "… subject testing to x@y" keeps them
    #    apart: the subject ends where the recipient marker begins.
    subject = ""
    subject_match = _SUBJECT.search(text)
    if subject_match:
        subject = _unquote(subject_match.group("subject"))
        text = f"{text[: subject_match.start()]} {text[subject_match.end():]}".strip()

    # 3. Recipient and body, in the order people speak them.
    recipient = ""
    body = ""
    to_match = _TO_MARKER.search(text)
    if to_match:
        recipient = _unquote(to_match.group("recipient"))
        remainder = f"{text[: to_match.start()]} {text[to_match.end():]}"
        say = _SAY_MARKER.search(remainder)
        body = _unquote(say.group("body")) if say else remainder
        text = "" if say else text[: to_match.start()]
    else:
        saying = _NAME_SAYING.match(text)
        if saying:
            recipient = _unquote(saying.group("name"))
            body = _unquote(saying.group("body"))
            text = ""
        else:
            quoted = re.findall(_QUOTED, text)
            if len(quoted) >= 2:
                recipient = _unquote(quoted[0])
                body = _unquote(quoted[1])
                text = ""
            else:
                # A two-word contact ("tejas classmate") cannot be guessed from
                # word order, so a known contact name is matched as the LONGEST
                # prefix first; whatever follows it is the message.
                contact = next(
                    (
                        name
                        for name in sorted(
                            {c.strip().lower() for c in known_contacts if c.strip()},
                            key=len,
                            reverse=True,
                        )
                        if text.lower().startswith(name + " ")
                    ),
                    "",
                )
                if contact:
                    recipient = text[: len(contact)]
                    body = text[len(contact) + 1 :]
                else:
                    pair = _PLAIN_PAIR.match(text)
                if not contact:
                    if pair:
                        recipient = _unquote(pair.group("recipient"))
                        body = _unquote(pair.group("body"))
                    else:
                        body = text

    body = _TRAILING_NOUN.sub("", _strip_edges(_unquote(body)))
    body = _strip_edges(body)
    recipient = _strip_edges(recipient)

    if not (app or recipient or body):
        return None
    return MessageRequest(app=app, recipient=recipient, subject=subject, body=body)


__all__ = ["MESSAGE_APPS", "MessageRequest", "parse_message_request"]
