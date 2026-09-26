"""The vision manager: read first, think only when reading was not enough.

This is the layer Phase 6 exists for. It answers one question — *what is in this
image, and does it answer what was asked?* — and it answers it in the cheapest
way that is honest::

    image -> OCR (cheap, deterministic) -> is the TEXT enough?
                 |                              |
                 | yes                          | no
                 v                              v
        structured result from text      vision model (if one is wired)
                                                |
                                                v
                                    structured result from pixels

Two rules make that more than a diagram.

**Reading is not a fallback, it is the first attempt.** A screenshot of an error
dialog is *text*, and the question "what is this error?" is answered by that
text. Paying a vision model for it — tens of seconds on a CPU-only machine, and
token by token — is the waste this layer exists to prevent. So OCR runs first,
and the model is consulted only when the text cannot answer: the question is not
about text, the text does not contain what was asked about ("where is the login
button?" needs the pixels, not the words), or nothing readable was found at all.

**A refusal is a result.** With no vision model wired, an unreadable image, or a
question that needs eyes, the pipeline returns a structured result that says the
question was *not* answered and why. It never promotes a guess to an answer, and
it never labels a text extraction as if a model had looked at the picture.

What leaves is ``VisionResult``: structured, bounded, and carrying its own
provenance — which reader answered, whether the model was escalated to, and what
the confidence number actually means. The model's reasoning is never part of it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.intelligence.semantic import content_words
from novacontrol.vision.models import (
    VisionElement,
    VisionImageType,
    VisionRequest,
    VisionResult,
    VisionTaskKind,
)
from novacontrol.vision.ocr import OcrEngine, OcrWord, default_ocr_engine, group_lines
from novacontrol.vision.providers import (
    NullVisionProvider,
    VisionProvider,
    VisionProviderError,
    provider_status,
)

#: Lines that look like a machine telling a person something went wrong. Used for
#: STRUCTURING the text, never for judging the picture: a screenshot containing
#: the word "error" is classified as an error screenshot because the text says
#: so, which is a fact about the image rather than a model's impression of it.
_ERROR_LINE = re.compile(
    r"\b(?:error|exception|traceback|failed|failure|warning|denied|refused"
    r"|not found|unavailable|timed out|timeout|fatal|invalid)\b"
    # An exception CLASS name is the canonical failure line ("TypeError:",
    # "ZeroDivisionError", "IOError") and the word "error" inside it has no word
    # boundary, so the plain rule above misses it. This is the shape a person
    # screenshots when they ask "what is this error?", so it is recognised.
    r"|\b\w+(?:Error|Exception)\b",
    re.IGNORECASE,
)

#: A question that is really "give me the text" — the pure-OCR case.
_TEXT_QUESTION = re.compile(
    r"\b(?:read|extract|transcribe|ocr|list the text|what does it say"
    r"|what does this say|what does (?:this|the) (?:image|screenshot|picture|photo) say"
    r"|text in|the text on)\b",
    re.IGNORECASE,
)

#: "Where is the login button?" — an element to FIND, which is a question about
#: where pixels are rather than what words say.
_LOCATE_QUESTION = re.compile(
    r"\b(?:where(?:'s| is| are)?|locate|find)\b[^?]*"
    r"\b(?:button|icon|menu|field|tab|link|checkbox|toggle|option|item|control)\b",
    re.IGNORECASE,
)
_SCREENSHOT_HINT = re.compile(r"\b(?:screenshot|screen ?shot|capture|screen)\b", re.IGNORECASE)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"})
_DOCUMENT_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".doc", ".docx", ".rtf", ".csv", ".log"})

#: The understand prompt asks for the documented structure directly, so a model
#: that can follow instructions returns something the pipeline uses as data
#: instead of re-parsing. A model that answers in prose is still accepted — the
#: prose becomes the summary — because a stricter contract would turn a usable
#: answer into a failure.
_UNDERSTAND_PROMPT = (
    "Look at this image and answer the question about it.\n"
    "Question: {question}\n\n"
    "Answer from what is actually visible, and say so when the image does not "
    "show it. Reply with a single JSON object and nothing else:\n"
    '{{"summary": "one or two sentences", '
    '"image_type": "application_screenshot|error_screenshot|document|photo|unknown", '
    '"detected_text": ["visible text, one string per line"], '
    '"ui_elements": [{{"label": "Login", "kind": "button", '
    '"bounds": {{"x": 0, "y": 0, "width": 0, "height": 0}}}}], '
    '"errors": ["any error or warning text that is visible"], '
    '"relevant_regions": [{{"label": "what is here", "x": 0, "y": 0}}], '
    '"confidence": 0.0}}'
)

_LOCATE_PROMPT = (
    "Find this element in the image: {target}\n"
    "If it is visible, reply with ONLY a JSON object "
    '{{"found": true, "x": <0-1000>, "y": <0-1000>, "label": "{target}"}} '
    "where x and y are on a normalized 0-1000 grid (resolution independent).\n"
    'If it is NOT visible, reply with ONLY {{"found": false}}.'
)


def task_for_question(question: str, *, target: str = "") -> VisionTaskKind:
    """Which kind of question this is, so the cheap path is chosen by MEANING.

    Read the text of the image -> a text task (OCR answers it, no model).
    Where is a named control -> a locator task (the pixels are the answer, so
    a text extraction cannot satisfy it and the model gets its turn).
    Anything else -> understand, where the only way to know whether OCR
    suffices is to read the image and compare against the question.

    Callers may always be explicit — an attached image with no question at all
    is an UNDERSTAND request — this only reads what the words already say.
    """
    asked = " ".join(str(question or "").split())
    if not asked and not str(target or "").strip():
        return VisionTaskKind.UNDERSTAND
    if str(target or "").strip():
        return VisionTaskKind.LOCATE
    if _LOCATE_QUESTION.search(asked):
        return VisionTaskKind.LOCATE
    if _TEXT_QUESTION.search(asked):
        return VisionTaskKind.OCR
    return VisionTaskKind.UNDERSTAND


def classify_image_type(
    source: str,
    *,
    question: str = "",
    errors: Sequence[str] = (),
) -> VisionImageType:
    """What kind of picture this is, from the filename and what was read.

    Deliberately cheap and deterministic: this is a LABEL for downstream
    routing and display, and a label produced by a model would be a second
    inference paid for before the first question is even asked. Text that
    mentions a failure is the strongest signal available here, so it decides
    first; then the name a capture was saved under; then the file type.
    """
    text = f"{source} {question}"
    if errors:
        return VisionImageType.ERROR_SCREENSHOT
    if _SCREENSHOT_HINT.search(text):
        return VisionImageType.APPLICATION_SCREENSHOT
    suffix = _suffix(source)
    if suffix in _DOCUMENT_SUFFIXES:
        return VisionImageType.DOCUMENT
    if suffix in _IMAGE_SUFFIXES:
        return VisionImageType.PHOTO
    return VisionImageType.UNKNOWN


def question_terms(question: str) -> tuple[str, ...]:
    """The words a question is ABOUT, lowercased and depunctuated.

    Reuses the NLU layer's own stopword list rather than a second one, so
    "what is this error?" is about ``error`` and not about ``what``.
    """
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    return tuple(word for word in content_words(normalized) if len(word) > 1)


#: The shortest question term allowed to match INSIDE a word rather than as a
#: whole one. Four is deliberately not one: "error" must find "ValueError" (the
#: question a person asks about a traceback, versus the token the traceback
#: prints), while a three-letter fragment is allowed to match far too much.
_MIN_SUBSTRING_TERM = 4

#: What finding a WORD is worth. A label read at a known position is a real
#: answer to "where is it" but not proof the word is the control that was asked
#: about, so the element list and a located answer quote the same figure rather
#: than each inventing one.
_LOCATED_WORD_CONFIDENCE = 0.5

#: What a model answer is worth when the model gave no figure of its own. It is
#: not a 50% claim — it is "answered, unquantified", and ``confidence_basis``
#: says exactly that so no caller can mistake it for a measurement.
_UNQUANTIFIED_MODEL_CONFIDENCE = 0.5

#: The nouns a person attaches to a label when asking where something is
#: ("the login BUTTON"). They name the kind of control, not the words on it, so
#: they are dropped before the label is looked for among the read words —
#: otherwise "login button" never matches the word "Login" that is on screen.
_CONTROL_NOUNS = frozenset({
    "button",
    "icon",
    "menu",
    "field",
    "tab",
    "link",
    "checkbox",
    "toggle",
    "option",
    "item",
    "control",
    "input",
    "box",
    "dropdown",
    "slider",
    "label",
})


def _text_tokens(text: str) -> tuple[str, ...]:
    """The text as lowercased alphanumeric tokens, for term matching."""
    return tuple(re.findall(r"[a-z0-9]+", text.lower()))


def _term_present(term: str, text: str, tokens: tuple[str, ...]) -> bool:
    """Whether the text says this term, as a whole word or as part of one.

    Whole word first, because that is the strict reading. Then, for a term long
    enough to be meaningful, a substring match — which is how "error" finds
    ``ValueError`` and "login" finds ``loginButton``. Without the second rule
    the canonical vision question ("what is this error?") escalated to a model
    on a screenshot whose text plainly contained the answer.
    """
    if re.search(rf"\b{re.escape(term)}\b", text):
        return True
    if len(term) < _MIN_SUBSTRING_TERM:
        return False
    return any(term in token for token in tokens)


def text_answers_question(question: str, text: str) -> bool:
    """Whether the OCR text contains what the question asked about.

    The honest version of "can this be solved from extracted text?": every
    content word of the question must appear in the text. A question with no
    content words at all is NOT answered this way — "what's on my screen?" is
    not a text extraction, and pretending otherwise would return a wall of text
    labelled as an answer.
    """
    terms = question_terms(question)
    if not terms or not text.strip():
        return False
    normalized = f" {re.sub(r'[^a-z0-9\s]', ' ', text.lower())} "
    tokens = _text_tokens(text)
    return all(_term_present(term, normalized, tokens) for term in terms)


def question_coverage(question: str, text: str) -> float:
    """The fraction of the question's content words the text actually contains.

    This is the OCR route's confidence, and it is a MEASURED number rather than
    a band someone liked: it falls out of comparing the question to the text, so
    a caller can see exactly why a result is less than certain.
    """
    terms = question_terms(question)
    if not terms:
        return 0.0
    normalized = f" {re.sub(r'[^a-z0-9\s]', ' ', text.lower())} "
    tokens = _text_tokens(text)
    found = sum(1 for term in terms if _term_present(term, normalized, tokens))
    return found / len(terms)


def extract_errors(lines: Sequence[str]) -> tuple[str, ...]:
    """The lines that read as a failure, in the order they appeared."""
    seen: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and _ERROR_LINE.search(stripped) and stripped not in seen:
            seen.append(stripped)
    return tuple(seen)


def read_json_object(answer: str) -> Mapping[str, Any] | None:
    """The first JSON object in a model's answer, tolerantly.

    Models wrap JSON in code fences, lead with a sentence, or trail a
    sign-off. Scanning for the first object that parses handles all three; a
    genuinely non-JSON answer returns ``None`` so the caller can fall back to
    treating the prose as a summary instead of failing.
    """
    text = answer.strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            character = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, Mapping):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


class VisionManager:
    """The single entry point for "what is in this image?".

    The provider and the OCR engine are both injected, which is what makes the
    layer testable without a model, a screen or a GPU — and what makes
    "replaceable provider" a fact about the code rather than a claim in a
    document. Swapping models is ``set_provider``; swapping OCR is the engine
    passed at construction.
    """

    def __init__(
        self,
        *,
        provider: VisionProvider | None = None,
        ocr: OcrEngine | None = None,
        prefer_ocr: bool = True,
        max_regions: int = 12,
    ) -> None:
        self._provider: VisionProvider = provider or NullVisionProvider()
        self._ocr: OcrEngine = ocr or default_ocr_engine()
        #: Whether the cheap path is tried first. Configurable because a caller
        #: that KNOWS it needs the model ("describe this photo") should not pay
        #: for an OCR pass it will discard.
        self.prefer_ocr = prefer_ocr
        #: A ceiling on structured regions, so one busy screenshot cannot
        #: return more markup than the answer it accompanies.
        self.max_regions = max(1, max_regions)

    # ── wiring ───────────────────────────────────────────────────────────
    @property
    def provider(self) -> VisionProvider:
        return self._provider

    @property
    def ocr(self) -> OcrEngine:
        return self._ocr

    def set_provider(self, provider: VisionProvider | None) -> None:
        """Hot-swap the vision model — the seam the panel and config use."""
        self._provider = provider or NullVisionProvider()

    def status(self) -> Mapping[str, Any]:
        """What this layer would do right now, for a status surface."""
        return {
            "provider": dict(provider_status(self._provider)),
            "ocr": {"name": self._ocr.name, "available": self._ocr.available},
            "prefer_ocr": self.prefer_ocr,
        }

    # ── the pipeline ─────────────────────────────────────────────────────
    async def analyze(self, request: VisionRequest) -> VisionResult:
        """Read the image, answer if the text suffices, else ask the model."""
        word_records: tuple[OcrWord, ...] = ()
        if request.prefer_ocr or request.task is VisionTaskKind.OCR:
            word_records = await self._ocr.read(request.source)
        lines = group_lines(word_records)
        text = "\n".join(lines)
        errors = extract_errors(lines)
        image_type = (
            request.image_type
            if request.image_type is not VisionImageType.UNKNOWN
            else classify_image_type(request.source, question=request.question, errors=errors)
        )
        if request.task is VisionTaskKind.OCR:
            return self._from_text(
                request,
                words=word_records,
                image_type=image_type,
                lines=lines,
                errors=errors,
                answered=bool(lines),
                reason="" if lines else self._no_text_reason(),
                basis=self._TEXT_EXTRACTION,
            )
        if request.task is VisionTaskKind.LOCATE:
            # "Where is the login button?" is answered from the text when the
            # text carries POSITIONS: OCR words know where they were read, so a
            # located label IS the answer, and paying a model for a point the
            # read already produced is exactly the waste this layer exists to
            # prevent. A label the text did not place escalates — "the word
            # appears somewhere" is not a place.
            located = self._locate_from_words(request, word_records)
            if located is not None:
                return self._from_text(
                    request,
                    words=word_records,
                    image_type=image_type,
                    lines=lines,
                    errors=errors,
                    answered=True,
                    reason="",
                    basis=self._LOCATE,
                    located=located,
                )
        elif self._should_answer_from_text(request, text):
            return self._from_text(
                request,
                words=word_records,
                image_type=image_type,
                lines=lines,
                errors=errors,
                answered=True,
                reason="",
                basis=(
                    self._TEXT_EXTRACTION
                    if _TEXT_QUESTION.search(request.question)
                    else "ocr-question-coverage"
                ),
            )
        return await self._escalate(
            request,
            words=word_records,
            image_type=image_type,
            lines=lines,
            errors=errors,
            text=text,
        )

    def _should_answer_from_text(self, request: VisionRequest, text: str) -> bool:
        """Whether OCR alone is enough — the spec's "yes" branch.

        A locate task never reaches here: it is answered by
        :meth:`_locate_from_words` when the text carries a position for the
        label, and escalates otherwise.
        """
        if not request.prefer_ocr:
            return False
        if request.task is VisionTaskKind.LOCATE:
            return False
        if not text.strip():
            return False
        if _TEXT_QUESTION.search(request.question):
            return True
        return text_answers_question(request.question, text)

    async def _escalate(
        self,
        request: VisionRequest,
        *,
        words: tuple[OcrWord, ...],
        image_type: VisionImageType,
        lines: tuple[str, ...],
        errors: tuple[str, ...],
        text: str,
    ) -> VisionResult:
        """The model's turn — or the honest refusal when there is no model."""
        if not request.allow_vlm:
            return self._from_text(
                request,
                words=words,
                image_type=image_type,
                lines=lines,
                errors=errors,
                answered=False,
                reason="this request is text-only, so no vision model was used",
                basis="vision-not-allowed",
            )
        if not self._provider.available:
            return self._from_text(
                request,
                words=words,
                image_type=image_type,
                lines=lines,
                errors=errors,
                answered=False,
                # The text that WAS read is in the result, so the refusal must
                # not claim the image went unread: what did not happen is the
                # PICTURE being looked at, which is a different statement.
                reason=(
                    "no vision model is wired, so the pixels were not looked at"
                    if lines
                    else self._no_text_reason()
                ),
                basis="no-vision-provider",
            )
        prompt = self._prompt_for(request)
        try:
            answer = await self._provider.see(prompt=prompt, image_path=request.source)
        except VisionProviderError as exc:
            # The model WAS consulted and could not answer, so the result says
            # so: `escalated` is True even though nothing came back, and the
            # answer source is `none` rather than `ocr` — the OCR text was read
            # for context, not used to answer, and claiming otherwise would
            # credit the wrong reader for an answer nobody produced.
            return self._from_text(
                request,
                words=words,
                image_type=image_type,
                lines=lines,
                errors=errors,
                answered=False,
                reason=str(exc),
                basis="vision-provider-error",
                escalated=True,
                answer_source="none",
            )
        del text
        return self._from_model(
            request, answer=answer, image_type=image_type, lines=lines, errors=errors
        )

    # ── result construction ──────────────────────────────────────────────
    #: The basis name for "the request was 'give me the text', and here it is".
    _TEXT_EXTRACTION = "ocr-text-extraction"
    #: The basis name for "the label was read, and here is where it was".
    _LOCATE = "ocr-locate"

    def _from_text(
        self,
        request: VisionRequest,
        *,
        words: tuple[OcrWord, ...],
        image_type: VisionImageType,
        lines: tuple[str, ...],
        errors: tuple[str, ...],
        answered: bool,
        reason: str,
        basis: str,
        escalated: bool = False,
        answer_source: str | None = None,
        located: OcrWord | None = None,
    ) -> VisionResult:
        """A result built from what the OCR engine actually read.

        ``escalated`` says whether a vision model was CONSULTED, not whether it
        answered: a provider that was tried and failed is provenance a caller
        needs, and only ``answer_source`` says who actually produced the text.

        ``located`` is the word the reader found for a locate question, and it
        is the one thing OCR can contribute to "where is it": a position. Its
        presence is what turns the elements list from context into an answer.
        """
        text = "\n".join(lines)
        summary = text if text else f"No text could be read from {request.source}."
        metadata: dict[str, Any] = {
            "answer_source": answer_source or ("ocr" if lines else "none"),
            "ocr_engine": self._ocr.name,
            "provider": self._provider.name,
            "escalated": escalated,
            "confidence_basis": basis,
        }
        if request.question:
            metadata["question"] = request.question
            metadata["question_answered"] = answered
            metadata["question_coverage"] = round(question_coverage(request.question, text), 3)
        if reason:
            metadata["reason"] = reason
        return VisionResult(
            image_type=image_type,
            summary=summary,
            detected_text=lines,
            ui_elements=self._elements_from_words(words),
            errors=errors,
            relevant_regions=_region_for(located),
            confidence=self._text_confidence(
                request, text, answered=answered, basis=basis, located=located
            ),
            answered=answered,
            metadata=metadata,
            escalated=escalated,
        )

    def _text_confidence(
        self,
        request: VisionRequest,
        text: str,
        *,
        answered: bool,
        basis: str,
        located: OcrWord | None = None,
    ) -> float:
        """How sure an OCR answer is, measured against what was actually asked.

        When the question was "give me the text", the coverage of its content
        words is the wrong measure — the words of a request are not claims about
        the image, so "read the text in this screenshot" scored 0.0 against text
        that quoted it perfectly. An extraction that produced text is a complete
        answer to that request; anything else is scored by how much of the
        question the text really covers.
        """
        if not answered:
            return 0.0
        if basis == self._TEXT_EXTRACTION:
            return 1.0
        if located is not None:
            # A located word is worth what any located word is worth: finding
            # the label is not the same as knowing it is the control that was
            # asked about, and a higher number would not have been earned.
            return _LOCATED_WORD_CONFIDENCE
        return question_coverage(request.question, text)

    def _locate_from_words(
        self, request: VisionRequest, words: tuple[OcrWord, ...]
    ) -> OcrWord | None:
        """The read word that IS the label asked about, when it has a position.

        Only a word the reader PLACED can answer "where is it", so a match with
        no geometry is no match — a plain text file knows its words and not
        where they were drawn, and answering from it would return the origin as
        a click target. Candidates are the label's own content words, longest
        first, because "sign in" must prefer ``Sign`` over ``in``; the match is
        exact-cased-folded, then whole-word, then containment for a long enough
        word, which is how "login" finds ``Login`` and ``loginButton``.
        """
        label = " ".join(str(request.target or request.question or "").split()).strip()
        if not label:
            return None
        candidates = _label_terms(label)
        if not candidates:
            return None
        placed = [word for word in words if word.has_geometry and word.text.strip()]
        if not placed:
            return None
        for term in candidates:
            match = _best_word(placed, term)
            if match is not None:
                return match
        return None

    def _elements_from_words(self, words: tuple[OcrWord, ...]) -> tuple[VisionElement, ...]:
        """Elements the OCR geometry can prove, and nothing more.

        Only words carrying real coordinates qualify, so an engine that reports
        text without geometry contributes no elements rather than elements at
        the origin. Labels are the words themselves — a text word is not a
        button, and this method does not pretend to know which words are.
        """
        if not any(word.has_geometry for word in words):
            return ()
        elements: list[VisionElement] = []
        for word in words[: self.max_regions]:
            if not word.has_geometry:
                continue
            elements.append(
                VisionElement(
                    label=word.text,
                    kind="text",
                    bounds={
                        "x": word.x,
                        "y": word.y,
                        "width": word.width,
                        "height": word.height,
                    },
                    confidence=_LOCATED_WORD_CONFIDENCE,
                )
            )
        return tuple(elements)

    def _from_model(
        self,
        request: VisionRequest,
        *,
        answer: str,
        image_type: VisionImageType,
        lines: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> VisionResult:
        """A result built from the model's answer, structured when it allows."""
        parsed = read_json_object(answer)
        elements: tuple[VisionElement, ...] = ()
        regions: tuple[Mapping[str, Any], ...] = ()
        reported: float | None = None
        found: bool | None = None
        summary = answer.strip()
        if parsed is not None:
            summary = str(parsed.get("summary") or summary).strip()
            elements = _elements_from_json(parsed.get("ui_elements"), self.max_regions)
            regions = _regions_from_json(parsed.get("relevant_regions"), self.max_regions)
            model_errors = _strings_from_json(parsed.get("errors"))
            if model_errors:
                errors = tuple(dict.fromkeys((*errors, *model_errors)))
            model_text = _strings_from_json(parsed.get("detected_text"))
            if model_text and not lines:
                lines = model_text
            declared = str(parsed.get("image_type") or "").strip()
            for candidate in VisionImageType:
                if candidate.value == declared:
                    image_type = candidate
                    break
            reported = _as_float(parsed.get("confidence"))
            found = _found_flag(parsed)
            # The LOCATE contract answers with a point rather than a list, so a
            # "found" answer becomes the one region (and the one element) it is:
            # the label at that spot. A "not found" answer is still an ANSWER —
            # the model looked and the element was not there.
            point = _point_from_json(parsed) if not regions else None
            label = ""
            if point is not None:
                # A label the model gave, or the thing the question named — the
                # QUESTION's own words are not a label, so "where is it?" never
                # produces "where is it? is visible at …".
                label = str(parsed.get("label") or request.target or "").strip()
                label = label or _asked_about(request) or "The element"
                regions = (
                    {"label": label, "x": point[0], "y": point[1], "grid": "0-1000"},
                )
                elements = (
                    *elements,
                    VisionElement(
                        label=label,
                        kind="target",
                        bounds={"x": int(round(point[0])), "y": int(round(point[1]))},
                        confidence=reported or 0.0,
                    ),
                )
            if parsed.get("summary") is None:
                # The contract's own JSON is a WIRE format. When the model used
                # the contract without a summary, what a person reads is built
                # from what was actually determined — never the raw object.
                summary = _contract_summary(request, point=point, label=label, found=found)
        answered = bool(summary or elements or regions) or found is not None
        metadata: dict[str, Any] = {
            "answer_source": self._provider.name,
            "ocr_engine": self._ocr.name,
            "provider": self._provider.name,
            "escalated": True,
            "structured": parsed is not None,
            "confidence_basis": "model-reported" if reported is not None else "none",
        }
        if found is not None:
            metadata["found"] = found
        if request.question:
            metadata["question"] = request.question
            metadata["question_answered"] = answered
        if not answered:
            metadata["reason"] = "the vision model returned nothing usable"
        return VisionResult(
            image_type=image_type,
            summary=summary,
            detected_text=lines,
            ui_elements=elements,
            errors=errors,
            relevant_regions=regions,
            confidence=self._model_confidence(reported, answered=answered, found=found),
            answered=answered,
            escalated=True,
            metadata=metadata,
        )

    @staticmethod
    def _model_confidence(
        reported: float | None, *, answered: bool, found: bool | None
    ) -> float:
        """What a model answer's confidence is, without inventing a number.

        A figure the model actually reported is used as given. A "not visible"
        answer is 0.0 — the model looked and there is no position to be
        confident about, and quoting a middle figure beside an empty region
        list would read as a located element. Anything else is the model's own
        answer with no figure attached, so the pipeline says so with one named
        constant and ``confidence_basis: "none"`` rather than pretending to a
        measurement it never made.
        """
        if reported is not None:
            return reported
        if found is False:
            return 0.0
        return _UNQUANTIFIED_MODEL_CONFIDENCE if answered else 0.0

    def _prompt_for(self, request: VisionRequest) -> str:
        if request.task is VisionTaskKind.LOCATE:
            target = request.target or request.question or "the element asked about"
            return _LOCATE_PROMPT.format(target=target)
        asked = " ".join(str(request.question or "").split())
        if not asked:
            asked = "Describe what is visible, and any errors or warnings."
        return _UNDERSTAND_PROMPT.format(question=asked)

    def _no_text_reason(self) -> str:
        if not self._ocr.available:
            return f"no OCR engine is available (tried {self._ocr.name})"
        return "no readable text was found in the image"


def _label_terms(label: str) -> tuple[str, ...]:
    """The words to look for, from a label a person wrote.

    Whole words longer than one character, the control nouns removed, longest
    first — so the same input always produces the same search order. A label
    that is nothing BUT a control noun ("the button") keeps its words rather
    than becoming an empty search: looking for "button" is at worst a longer
    shot, while looking for nothing is a guaranteed miss.
    """
    words = [word for word in re.findall(r"[A-Za-z0-9]+", label) if len(word) > 1]
    content = [word for word in words if word.lower() not in _CONTROL_NOUNS]
    if not content:
        content = words
    return tuple(sorted(content, key=lambda word: (-len(word), word.lower())))


def _best_word(words: Sequence[OcrWord], term: str) -> OcrWord | None:
    """The read word that IS this term — exactly, then loosely, then not at all."""
    folded = term.casefold()
    for word in words:
        if word.text.strip().casefold() == folded:
            return word
    for word in words:
        if re.search(rf"\b{re.escape(term)}\b", word.text, re.IGNORECASE):
            return word
    if len(term) < _MIN_SUBSTRING_TERM:
        return None
    for word in words:
        if folded in word.text.casefold():
            return word
    return None


def _region_for(word: OcrWord | None) -> tuple[Mapping[str, Any], ...]:
    """The located word as one region, or nothing when nothing was located.

    The region carries the word's own pixel bounds AND its centre, because the
    two answer different questions: a caller drawing attention wants the box,
    and a caller about to click wants the point. No scale is invented — the
    coordinates are the reader's.
    """
    if word is None:
        return ()
    x, y = word.center
    return (
        {
            "label": word.text,
            "x": word.x,
            "y": word.y,
            "width": word.width,
            "height": word.height,
            "center_x": x,
            "center_y": y,
            "source": word.kind,
        },
    )


#: Words that point at something without naming it. A question made only of
#: these ("where is it?") has no label in it, and quoting one back would read as
#: though the pipeline had read the word "it" off the screen.
_POINTING_WORDS = frozenset({
    "it",
    "this",
    "that",
    "these",
    "those",
    "them",
    "there",
    "thing",
    "element",
    "button",
    "one",
})


def _asked_about(request: VisionRequest) -> str:
    """What the question was about, as a noun phrase a sentence can use.

    Returns an empty string when the question names nothing ("where is it?"),
    so a caller falls back to a neutral phrase rather than to the question.
    """
    label = " ".join(str(request.target or request.question or "").split()).strip()
    label = re.sub(r"[?!.]+$", "", label).strip()
    if label.lower().startswith(("where is ", "where's ", "where are ", "find ", "locate ")):
        label = re.sub(
            r"^(?:where(?:'s| is| are)|find|locate)\s+", "", label, flags=re.IGNORECASE
        )
        label = re.sub(r"^(?:the|my)\s+", "", label, flags=re.IGNORECASE).strip()
    if not label or label.lower() in _POINTING_WORDS:
        return ""
    return label


def _contract_summary(
    request: VisionRequest,
    *,
    point: tuple[float, float] | None,
    label: str,
    found: bool | None,
) -> str:
    """A sentence for an answer that came back in the locate contract.

    Three cases, all of which used to reach the caller as the raw JSON object:
    a point was given, the model affirmed the element without a position, or the
    model said it was not there. The grid is named where a point is quoted,
    because a bare pair of numbers means nothing without its scale.
    """
    if point is not None:
        where = label or _asked_about(request) or "The element"
        return f"{where} is visible at ({point[0]:.0f}, {point[1]:.0f}) on a 0-1000 grid."
    if found is True:
        return "The element was reported as visible, but no position was given."
    if found is False:
        asked = _asked_about(request)
        if asked:
            return f"No {asked} is visible in the image."
        return "The element asked about is not visible in the image."
    return "The model answered, but in a shape this pipeline cannot read."


def _suffix(source: str) -> str:
    _, _, tail = str(source).rpartition(".")
    return f".{tail.lower()}" if tail and len(tail) <= 5 else ""


def _elements_from_json(value: Any, limit: int) -> tuple[VisionElement, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    elements: list[VisionElement] = []
    for item in value[:limit]:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        if not label:
            continue
        raw_bounds = item.get("bounds")
        bounds: Mapping[str, Any] = raw_bounds if isinstance(raw_bounds, Mapping) else {}
        elements.append(
            VisionElement(
                label=label,
                kind=str(item.get("kind") or "element").strip() or "element",
                bounds={str(key): _as_int(value) for key, value in bounds.items()},
                confidence=_as_float(item.get("confidence")) or 0.0,
            )
        )
    return tuple(elements)


def _regions_from_json(value: Any, limit: int) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    regions: list[Mapping[str, Any]] = []
    for item in value[:limit]:
        if isinstance(item, Mapping):
            regions.append(dict(item))
    return tuple(regions)


def _strings_from_json(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes,)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _as_number(value: Any) -> float | None:
    """A coordinate or count, as a number — no 0..1 clamping (that is confidence)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point_from_json(data: Mapping[str, Any]) -> tuple[float, float] | None:
    """A normalized point from the locate contract, if the answer carries one."""
    x = _as_number(data.get("x"))
    y = _as_number(data.get("y"))
    if x is None or y is None:
        return None
    return x, y


def _found_flag(data: Mapping[str, Any]) -> bool | None:
    """The locate contract's ``found``, when the answer is that contract."""
    value = data.get("found")
    if isinstance(value, bool):
        return value
    return None


def _as_int(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0
