"""The vision provider boundary — one seam, many possible viewers.

NovaControl must never be built around one VLM. A vision model is a component
with a lifetime: today's local option is a quantized build on a CPU, tomorrow's
might be a purpose-built VLM, a different family entirely, or a cloud endpoint.
All that matters to the pipeline is that something can look at an image and
answer a question about it.

So this module defines exactly that: ``VisionProvider``, a thing that can SEE.
It is deliberately narrow — one method, returning text — because everything
interesting (deciding what to ask, parsing the answer, structuring the result,
telling OCR from a model call) belongs to the manager, not to the seam. A
provider that fails is classified as ``VisionProviderError`` rather than
returning a plausible empty string, so "the model was unreachable" can never be
mistaken for "the model saw nothing".

The shipped implementation wraps an existing completion provider, which is how
a local Ollama vision model, a cloud preset and the Echo fallback all arrive
here without a new HTTP client. Which one is used is decided by configuration
(see ``core/config.py``: ``vision.provider`` / ``vision.model``) and validated
by the capability gate that already existed — ``provider_supports_vision``,
which refuses a text-only chat model rather than letting it invent coordinates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from novacontrol.integrations.llm import provider_supports_vision


class VisionProviderError(RuntimeError):
    """The provider could not answer — it never means "found nothing"."""


@runtime_checkable
class VisionProvider(Protocol):
    """Something that can look at an image and answer about it."""

    @property
    def name(self) -> str:
        """A short, stable identifier for status output ('none', 'llm:ollama')."""

    @property
    def model(self) -> str:
        """The model behind the provider, when it has a name."""

    @property
    def available(self) -> bool:
        """Whether this provider can actually see (a text model cannot)."""

    async def see(self, *, prompt: str, image_path: str) -> str:
        """Answer ``prompt`` about the image at ``image_path``.

        Raises ``VisionProviderError`` when the provider is unusable or the
        call fails — a caller must be able to tell that apart from an answer
        that simply said nothing.
        """


class NullVisionProvider:
    """No vision model: the honest default on a machine without one.

    It reports ``available = False`` rather than raising at construction, so the
    pipeline can carry on to the OCR answer and label the result truthfully.
    """

    name = "none"
    model = ""

    @property
    def available(self) -> bool:
        return False

    async def see(self, *, prompt: str, image_path: str) -> str:
        del prompt, image_path
        raise VisionProviderError("no vision model is wired")


class CompletionVisionProvider:
    """A vision provider backed by any object that can complete a message list.

    That is the same shape the existing multimodal processor already sends
    (OpenAI-style content blocks with an ``image_url``), so a local Ollama
    vision model, a cloud vision preset and a test double are all equally
    acceptable here. The image is prepared through
    ``_load_image_base64_for_vision`` — the existing downscale that keeps a
    capture inside the model's token budget — so every provider inherits that
    behaviour instead of each re-inventing it.
    """

    def __init__(
        self,
        provider: object,
        *,
        name: str = "llm",
        model: str = "",
        max_tokens: int = 0,
    ) -> None:
        self._provider = provider
        self._name = name
        self._model = model
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model or _model_of(self._provider)

    @property
    def source(self) -> object | None:
        """The completion provider this wrapper calls through.

        Exposed so a caller can read the provider's own MEASUREMENTS — the timing
        breakdown of the last call lives on the object that made it, and the
        wrapper is otherwise the only one that knows which object that was. Not
        part of ``VisionProvider``: a provider that is not a wrapper has no inner
        provider, and requiring the attribute would make every implementer grow
        an empty one.
        """
        return self._provider

    @property
    def available(self) -> bool:
        return self._provider is not None and provider_supports_vision(self._provider)

    async def see(self, *, prompt: str, image_path: str) -> str:
        if not self.available:
            raise VisionProviderError("the configured provider cannot see images")
        if not prompt.strip():
            raise VisionProviderError("a vision question is required")
        # Local import: the multimodal module imports processors, and this
        # module is imported by processors' package sibling, so keeping the
        # image preparation out of module import time avoids a cycle.
        from novacontrol.vision.multimodal import _load_image_base64_for_vision

        image_data = _load_image_base64_for_vision(image_path)
        if not image_data:
            raise VisionProviderError(f"could not read an image at {image_path}")
        complete = getattr(self._provider, "complete", None)
        if complete is None:
            raise VisionProviderError("the vision provider has no complete method")
        try:
            answer = await complete(_vision_messages(prompt, image_data))
        except Exception as exc:  # noqa: BLE001 - classified, then reported
            raise VisionProviderError(f"vision call failed: {exc}") from exc
        return str(answer or "").strip()


def _vision_messages(prompt: str, image_data: str) -> list[dict[str, Any]]:
    """The OpenAI-style multimodal message the whole codebase already uses."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_data}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]


def build_vision_provider(
    provider: object | None,
    *,
    name: str = "",
    model: str = "",
) -> VisionProvider:
    """A provider for ``provider`` — or the honest null one.

    The capability gate is applied here rather than at the call site, so there
    is exactly one place in the codebase that decides what counts as eyes. A
    text-only chat model, the Echo fallback, and ``None`` all resolve to
    ``NullVisionProvider``.
    """
    if provider is None or not provider_supports_vision(provider):
        return NullVisionProvider()
    label = name or provider_id(provider)
    return CompletionVisionProvider(
        provider,
        name=f"llm:{label}" if label else "llm",
        model=model or _model_of(provider),
    )


def provider_id(provider: object) -> str:
    """A stable, human-readable id for a provider object.

    Checked in widening order: an explicit id (cloud presets carry one), then a
    declared name, then the class name reduced to something readable. Never
    raises — status output is not worth an exception.
    """
    for attribute in ("provider_id", "name", "kind"):
        value = getattr(provider, attribute, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return type(provider).__name__


def _model_of(provider: object) -> str:
    for attribute in ("model", "model_name", "deployment"):
        value = getattr(provider, attribute, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def provider_status(provider: VisionProvider) -> Mapping[str, Any]:
    """The provider as status output — never the credential."""
    return {
        "name": provider.name,
        "model": provider.model,
        "available": provider.available,
    }


def first_available(providers: Sequence[VisionProvider]) -> VisionProvider:
    """The first provider that can actually see, or the last (usually null)."""
    for provider in providers:
        if provider.available:
            return provider
    return providers[-1] if providers else NullVisionProvider()
