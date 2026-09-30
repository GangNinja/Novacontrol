"""One place that decides what may leave this machine.

Phase 14.3's requirement is organizational as much as functional: privacy checks
must not be scattered. So every outbound path asks THIS object, and the answer —
including the reason and the switch that produced it — is the same shape
wherever it is asked:

    cloud model        ->  policy.require(PrivacyAction.CLOUD_MODEL)
    external search    ->  policy.allows(PrivacyAction.EXTERNAL_SEARCH)
    an external tool   ->  policy.allows(PrivacyAction.EXTERNAL_TOOL)
    outbound telemetry ->  policy.allows(PrivacyAction.TELEMETRY)

The policy has two layers, and the order matters:

1. **Execution mode.** ``LOCAL_ONLY`` closes every external path regardless of
   what the individual controls say; ``BALANCED`` and ``PERFORMANCE`` leave that
   to the controls. A mode that could be widened by a switch would not be a
   mode.
2. **The controls.** Each outbound action has one named switch. Off means no,
   with the switch named in the refusal so the operator knows what to change.

``sensitive_data_redaction`` is the third piece: when it is on,
:meth:`PrivacyPolicy.external_text` filters credentials out of text before it
is handed to an external path. The filter is the audit trail's own
:class:`~novacontrol.audit.redact.Redactor` — one implementation of "what looks
like a secret" rather than two that can drift, and the counts are returned so a
caller can record that a redaction happened without ever seeing the value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.optimization.models import (
    ExecutionMode,
    PrivacyAction,
    PrivacyControls,
    PrivacyDecision,
)

#: Which control governs which action. Kept as data so ``evaluate`` and any
#: future surface (a settings screen, a report) read the same mapping.
CONTROL_FOR_ACTION: dict[PrivacyAction, str] = {
    PrivacyAction.CLOUD_MODEL: "allow_cloud",
    PrivacyAction.REMOTE_MODEL: "allow_remote_model",
    PrivacyAction.EXTERNAL_SEARCH: "allow_external_search",
    PrivacyAction.EXTERNAL_TOOL: "allow_external_tools",
    PrivacyAction.TELEMETRY: "allow_telemetry",
}

#: Why the mode refused, per action, so the sentence names both the mode and
#: the path rather than "privacy".
_MODE_REASON: dict[PrivacyAction, str] = {
    PrivacyAction.CLOUD_MODEL: "a cloud model call",
    PrivacyAction.REMOTE_MODEL: "a remote model",
    PrivacyAction.EXTERNAL_SEARCH: "an external search",
    PrivacyAction.EXTERNAL_TOOL: "an external tool",
    PrivacyAction.TELEMETRY: "outbound telemetry",
}


class PrivacyDenied(PermissionError):
    """Raised by :meth:`PrivacyPolicy.require` when a path is closed.

    Subclasses ``PermissionError`` so existing callers that already understand
    a refusal (and the API's error handling) treat it as one, while carrying the
    structured :class:`PrivacyDecision` for a caller that wants the detail.
    """

    def __init__(self, decision: PrivacyDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class PrivacyPolicy:
    """The execution mode, the controls, and the central decision.

    Deliberately stateful and cheap to update: the application holds ONE policy,
    settings changes call :meth:`update`, and every call site keeps asking the
    same object. It performs no I/O — asking it is as cheap as reading a
    boolean, so a hot path does not need to cache an answer that the operator
    may have changed one second ago.
    """

    def __init__(
        self,
        controls: PrivacyControls | None = None,
        mode: ExecutionMode | str = ExecutionMode.BALANCED,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._controls = controls if controls is not None else PrivacyControls()
        self._mode = ExecutionMode.from_text(mode)
        self._redactor = redactor if redactor is not None else Redactor()

    # -- state --------------------------------------------------------------
    @property
    def controls(self) -> PrivacyControls:
        return self._controls

    @property
    def mode(self) -> ExecutionMode:
        return self._mode

    def update(
        self,
        *,
        controls: PrivacyControls | None = None,
        mode: ExecutionMode | str | None = None,
    ) -> dict[str, Any]:
        """Apply a settings change and return the resulting policy as a dict."""
        if controls is not None:
            self._controls = controls
        if mode is not None:
            self._mode = ExecutionMode.from_text(mode)
        return self.to_dict()

    def apply(
        self,
        *,
        mode: ExecutionMode | str | None = None,
        allow_cloud: bool | None = None,
        allow_external_search: bool | None = None,
        allow_external_tools: bool | None = None,
        allow_telemetry: bool | None = None,
        allow_remote_model: bool | None = None,
        sensitive_data_redaction: bool | None = None,
    ) -> dict[str, Any]:
        """Set individual controls; ``None`` means "leave as it is".

        A named-keyword form so a settings surface cannot accidentally reorder
        booleans, and so an update passes only what actually changed.
        """
        current = self._controls
        self._controls = PrivacyControls(
            allow_cloud=current.allow_cloud if allow_cloud is None else bool(allow_cloud),
            allow_external_search=(
                current.allow_external_search
                if allow_external_search is None
                else bool(allow_external_search)
            ),
            allow_external_tools=(
                current.allow_external_tools
                if allow_external_tools is None
                else bool(allow_external_tools)
            ),
            allow_telemetry=(
                current.allow_telemetry if allow_telemetry is None else bool(allow_telemetry)
            ),
            allow_remote_model=(
                current.allow_remote_model
                if allow_remote_model is None
                else bool(allow_remote_model)
            ),
            sensitive_data_redaction=(
                current.sensitive_data_redaction
                if sensitive_data_redaction is None
                else bool(sensitive_data_redaction)
            ),
        )
        if mode is not None:
            self._mode = ExecutionMode.from_text(mode)
        return self.to_dict()

    # -- the decision ---------------------------------------------------------
    def evaluate(self, action: PrivacyAction | str) -> PrivacyDecision:
        """Whether an outbound action may proceed, and the single reason why.

        The mode is checked first, then the control, and exactly one reason is
        returned even when both would refuse: the operator should be told the
        first thing to change, not handed a list of everything closed.
        """
        wanted = PrivacyAction(str(action))
        if not self._mode.external_allowed:
            return PrivacyDecision(
                allowed=False,
                action=wanted,
                reason=(
                    f"the execution mode is {self._mode.value}, which keeps "
                    f"{_MODE_REASON[wanted]} closed"
                ),
                control="execution_mode",
                mode=self._mode,
            )
        control = CONTROL_FOR_ACTION[wanted]
        allowed = bool(getattr(self._controls, control))
        reason = (
            f"{control} permits {wanted.value}"
            if allowed
            else f"{control} is off, so {wanted.value} is not permitted"
        )
        return PrivacyDecision(
            allowed=allowed,
            action=wanted,
            reason=reason,
            control=control,
            mode=self._mode,
        )

    def allows(self, action: PrivacyAction | str) -> bool:
        return self.evaluate(action).allowed

    def require(self, action: PrivacyAction | str) -> PrivacyDecision:
        """``evaluate``, but a refusal RAISES instead of returning.

        For the paths that must not proceed quietly: a cloud call that would
        otherwise fall back to a local one with no explanation, for instance.
        """
        decision = self.evaluate(action)
        if not decision.allowed:
            raise PrivacyDenied(decision)
        return decision

    # -- redaction ------------------------------------------------------------
    def external_text(self, text: str) -> tuple[str, int, tuple[str, ...]]:
        """Text as an external path may see it: ``(safe, redactions, kinds)``.

        With ``sensitive_data_redaction`` off this is the identity, because the
        operator chose that. With it on, the audit trail's redactor filters
        private-key blocks, authorization headers, bearer tokens, JWTs,
        ``sk-``/``ghp_``/``xox``/``AKIA`` credentials, credentials embedded in
        URLs and ``key=value`` assignments — the same rules the local audit
        trail already applies, so "what counts as a secret" has one answer in
        this codebase.
        """
        if not self._controls.sensitive_data_redaction:
            return str(text), 0, ()
        outcome = self._redactor.redact_text(str(text))
        return outcome.text, outcome.count, outcome.kinds

    # -- reporting ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            **self._mode.to_dict(),
            "controls": self._controls.to_dict(),
            "sensitive_data_redaction": self._controls.sensitive_data_redaction,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> PrivacyPolicy:
        """Build from a settings object (or mapping) that carries the controls.

        Reads the two vocabularies this build already persists — ``brain_mode``
        is NOT the execution mode and is deliberately not consulted here; the
        execution mode is its own setting (``execution_mode``).
        """
        if isinstance(settings, Mapping):
            data: Mapping[str, Any] = settings
        else:
            to_dict = getattr(settings, "to_dict", None)
            rendered = to_dict() if callable(to_dict) else {}
            data = rendered if isinstance(rendered, Mapping) else {}
        mode = ExecutionMode.from_text(
            data.get("execution_mode", ExecutionMode.BALANCED)
        )
        controls = PrivacyControls.from_mapping(
            {
                "allow_cloud": data.get("allow_cloud", PrivacyControls().allow_cloud),
                "allow_external_search": data.get(
                    "allow_external_search", PrivacyControls().allow_external_search
                ),
                "allow_external_tools": data.get(
                    "allow_external_tools", PrivacyControls().allow_external_tools
                ),
                "allow_telemetry": data.get(
                    "allow_telemetry", PrivacyControls().allow_telemetry
                ),
                "allow_remote_model": data.get(
                    "allow_remote_model", PrivacyControls().allow_remote_model
                ),
                "sensitive_data_redaction": data.get(
                    "sensitive_data_redaction",
                    PrivacyControls().sensitive_data_redaction,
                ),
            }
        )
        return cls(controls=controls, mode=mode)


__all__ = [
    "CONTROL_FOR_ACTION",
    "PrivacyDenied",
    "PrivacyPolicy",
]
