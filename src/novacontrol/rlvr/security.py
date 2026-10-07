"""The security boundaries that keep a reward worth learning from.

RLVR's promise only holds if the thing being rewarded cannot be edited by the
thing being rewarded. This module makes that concrete with four mechanisms, all
built on what already exists:

  * **Expected results are frozen.** An expectation is hashed when it is
    created and checked again when it is used; a mismatch is an error, not a
    new target.
  * **Verifiers are fingerprinted.** The registry already refuses to swap an
    implementation under an existing id+version; a run additionally pins the
    whole set it started with and can prove nothing was added, removed or
    changed.
  * **Reward configuration is pinned per run.** A reward built by a different
    configuration version than the run pins is refused by the validator.
  * **Actions that touch any of the above are refused outright.** The
    :class:`RewardTamperGuard` recognises the shapes — modify the verifier,
    change an expected result, disable verification, relax the reward policy —
    and returns a named refusal before the action reaches a tool.

Where an action is legitimate but risky, the decision goes through Phase 8's
:class:`~novacontrol.reliability.permissions.PermissionManager` rather than a
second safety layer, so "may this run?" has one answer in the system.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.core.security import RiskLevel
from novacontrol.evaluation.models import now_iso
from novacontrol.rlvr.config import VerifiableRewardConfig
from novacontrol.rlvr.models import _text, expected_fingerprint
from novacontrol.rlvr.registry import VerifierRegistry

#: Refusal codes a caller can act on.
SECURITY_PROTECTED_TARGET = "protected_target"
SECURITY_VERIFICATION_CONTROL = "verification_control"
SECURITY_EXPECTED_CHANGED = "expected_result_changed"
SECURITY_CONFIG_CHANGED = "reward_config_changed"
SECURITY_VERIFIERS_CHANGED = "verifiers_changed"
SECURITY_CONFIRMATION_REQUIRED = "confirmation_required"

#: Operations that would change protected state, and the targets they must not.
_CHANGING_VERBS: frozenset[str] = frozenset(
    {
        "modify",
        "edit",
        "patch",
        "rewrite",
        "write",
        "save",
        "delete",
        "remove",
        "unlink",
        "update",
        "set",
        "change",
        "disable",
        "bypass",
        "override",
        "replace",
    }
)

_PROTECTED_MARKERS: tuple[str, ...] = (
    "verifier",
    "expected",
    "reward_config",
    "reward policy",
    "reward policy config",
    "verification_state",
    "evaluation_state",
    "benchmark",
    "expected_output",
    "expected_result",
    "test expectation",
    "reward_weights",
)

#: Tools whose whole job is verification control: naming one is enough.
_VERIFICATION_CONTROL_TOOLS: frozenset[str] = frozenset(
    {
        "register_verifier",
        "unregister_verifier",
        "disable_verifier",
        "modify_verifier",
        "set_reward_config",
        "set_reward_weights",
        "disable_verification",
        "modify_expected",
        "set_expected_result",
        "reset_verification",
    }
)


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class ExpectedSnapshot:
    """An expectation as it was when the question was written."""

    snapshot_id: str = ""
    digest: str = ""
    expected: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def matches(self, expected: Mapping[str, Any]) -> bool:
        return expected_fingerprint(expected) == self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "digest": self.digest,
            "expected": dict(self.expected),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PinnedRewardConfig:
    """The reward policy a run started with, as a version and a fingerprint."""

    version: str = ""
    fingerprint: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def matches(self, config: VerifiableRewardConfig) -> bool:
        return (
            config.version == self.version
            and config.fingerprint() == self.fingerprint
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fingerprint": self.fingerprint,
            "config": dict(self.config),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SecurityVerdict:
    """The answer to "may this action proceed?", with its reason named."""

    allowed: bool = True
    code: str = ""
    reason: str = ""
    risk_level: str = RiskLevel.LOW.value
    requires_confirmation: bool = False
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    """A difference between a run's protection and the state now."""

    code: str
    severity: str = "error"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RunProtection:
    """The state a run pins: its verifier set and its reward configuration."""

    run_id: str = ""
    registry_snapshot: Mapping[str, str] = field(default_factory=dict)
    reward_config: PinnedRewardConfig = field(default_factory=PinnedRewardConfig)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "registry_snapshot": dict(self.registry_snapshot),
            "reward_config": self.reward_config.to_dict(),
            "created_at": self.created_at,
        }


class RewardTamperGuard:
    """Refuses an action whose target or tool is protected verification state."""

    def assess(self, action: Mapping[str, Any], *, tool: str = "") -> SecurityVerdict:
        verb = _text(
            action.get("action")
            or action.get("operation")
            or action.get("kind")
            or action.get("verb")
        ).lower()
        target = " ".join(
            _text(value).lower()
            for key, value in action.items()
            if key
            in {
                "target",
                "path",
                "resource",
                "name",
                "file",
                "module",
                "object",
                "field",
                "id",
            }
        )
        tool_name = _text(tool or action.get("tool") or action.get("capability")).lower()
        if tool_name in _VERIFICATION_CONTROL_TOOLS:
            return SecurityVerdict(
                allowed=False,
                code=SECURITY_VERIFICATION_CONTROL,
                reason=(
                    f"tool {tool_name!r} changes verification state; verification "
                    "is not something the policy under evaluation may reconfigure"
                ),
                risk_level=RiskLevel.CRITICAL.value,
                parameters={"tool": tool_name},
            )
        if verb in _CHANGING_VERBS and any(
            marker in target for marker in _PROTECTED_MARKERS
        ):
            return SecurityVerdict(
                allowed=False,
                code=SECURITY_PROTECTED_TARGET,
                reason=(
                    f"the action would {verb} protected verification state "
                    f"(target {target!r}); a policy may not edit the thing that "
                    "grades it"
                ),
                risk_level=RiskLevel.CRITICAL.value,
                parameters={"verb": verb, "target": target},
            )
        if any(marker in tool_name for marker in _PROTECTED_MARKERS) and verb in _CHANGING_VERBS:
            return SecurityVerdict(
                allowed=False,
                code=SECURITY_VERIFICATION_CONTROL,
                reason=(
                    f"tool {tool_name!r} would {verb} verification state; refusing"
                ),
                risk_level=RiskLevel.CRITICAL.value,
                parameters={"verb": verb, "tool": tool_name},
            )
        return SecurityVerdict(allowed=True, reason="no protected state is touched")


class VerificationSecurityPolicy:
    """Pins expectations, configurations and verifier sets; asks Phase 8."""

    def __init__(
        self,
        *,
        guard: RewardTamperGuard | None = None,
        permissions: Any = None,
    ) -> None:
        self.guard = guard if guard is not None else RewardTamperGuard()
        self.permissions = permissions

    # -- expectations ------------------------------------------------------------

    def snapshot_expected(self, expected: Mapping[str, Any]) -> ExpectedSnapshot:
        digest = expected_fingerprint(expected)
        return ExpectedSnapshot(
            snapshot_id=f"exp-{digest[:12]}",
            digest=digest,
            expected=dict(expected),
        )

    def expected_intact(
        self, snapshot: ExpectedSnapshot, expected: Mapping[str, Any]
    ) -> tuple[SecurityFinding, ...]:
        if snapshot.matches(expected):
            return ()
        return (
            SecurityFinding(
                code=SECURITY_EXPECTED_CHANGED,
                severity="error",
                detail=(
                    f"the expected result changed after snapshot {snapshot.snapshot_id} "
                    f"({snapshot.digest} != {expected_fingerprint(expected)})"
                ),
            ),
        )

    # -- reward configuration ------------------------------------------------------

    def pin_reward_config(self, config: VerifiableRewardConfig) -> PinnedRewardConfig:
        return PinnedRewardConfig(
            version=config.version,
            fingerprint=config.fingerprint(),
            config=config.to_mapping(),
        )

    def config_intact(
        self, pinned: PinnedRewardConfig, config: VerifiableRewardConfig
    ) -> tuple[SecurityFinding, ...]:
        findings: list[SecurityFinding] = []
        if config.version != pinned.version:
            findings.append(
                SecurityFinding(
                    code=SECURITY_CONFIG_CHANGED,
                    severity="error",
                    detail=(
                        f"the reward configuration version changed "
                        f"({pinned.version!r} -> {config.version!r})"
                    ),
                )
            )
        elif config.fingerprint() != pinned.fingerprint:
            findings.append(
                SecurityFinding(
                    code=SECURITY_CONFIG_CHANGED,
                    severity="error",
                    detail=(
                        "the reward configuration changed under the same version "
                        f"({pinned.fingerprint} -> {config.fingerprint()})"
                    ),
                )
            )
        return tuple(findings)

    # -- verifier sets ---------------------------------------------------------------

    def protect_run(
        self,
        run_id: str,
        *,
        registry: VerifierRegistry,
        config: VerifiableRewardConfig,
    ) -> RunProtection:
        return RunProtection(
            run_id=run_id,
            registry_snapshot=registry.snapshot(),
            reward_config=self.pin_reward_config(config),
        )

    def check_run(
        self,
        protection: RunProtection,
        *,
        registry: VerifierRegistry,
        config: VerifiableRewardConfig,
    ) -> tuple[SecurityFinding, ...]:
        findings: list[SecurityFinding] = []
        for problem in registry.assert_unmodified(protection.registry_snapshot):
            findings.append(
                SecurityFinding(
                    code=SECURITY_VERIFIERS_CHANGED,
                    severity="error",
                    detail=problem,
                )
            )
        findings.extend(self.config_intact(protection.reward_config, config))
        return tuple(findings)

    # -- authorization ---------------------------------------------------------------

    def authorize(self, action: Mapping[str, Any], *, tool: str = "") -> SecurityVerdict:
        """Refuse protected actions; route the rest through Phase 8.

        The guard runs FIRST and its refusal is final — a permission check must
        not be able to talk the system into letting a policy edit its own
        verifier. For everything else, the permission manager answers "how risky
        is this, and does a person need to be asked?" so there is still one
        safety vocabulary in the system.
        """
        verdict = self.guard.assess(action, tool=tool)
        if not verdict.allowed:
            return verdict
        if self.permissions is None:
            return verdict
        try:
            decision = self.permissions.check(tool, action=_text(action.get("action")))
        except Exception as exc:  # noqa: BLE001 - a broken policy is not an allow
            return SecurityVerdict(
                allowed=False,
                code=SECURITY_CONFIRMATION_REQUIRED,
                reason=f"the permission layer could not decide: {type(exc).__name__}: {exc}",
                risk_level=RiskLevel.HIGH.value,
            )
        allowed = bool(getattr(decision, "allow", True))
        requires = bool(getattr(decision, "requires_confirmation", False))
        risk = getattr(decision, "risk", RiskLevel.LOW)
        risk_name = risk.value if hasattr(risk, "value") else _text(risk, "low")
        if requires:
            return SecurityVerdict(
                allowed=False,
                code=SECURITY_CONFIRMATION_REQUIRED,
                reason=(
                    f"risk {risk_name}: this action needs a person's approval "
                    "before it runs"
                ),
                risk_level=risk_name,
                requires_confirmation=True,
            )
        return SecurityVerdict(
            allowed=allowed,
            code="" if allowed else SECURITY_CONFIRMATION_REQUIRED,
            reason=_text(getattr(decision, "reason", ""), "permitted"),
            risk_level=risk_name,
        )

    def findings_for(
        self,
        *,
        protection: RunProtection,
        registry: VerifierRegistry,
        config: VerifiableRewardConfig,
        expected: Sequence[tuple[ExpectedSnapshot, Mapping[str, Any]]] = (),
    ) -> tuple[SecurityFinding, ...]:
        """Everything wrong with a run's protections, in one pass."""
        findings = list(self.check_run(protection, registry=registry, config=config))
        for snapshot, current in expected:
            findings.extend(self.expected_intact(snapshot, current))
        return tuple(findings)

    @staticmethod
    def digest_of(value: Mapping[str, Any]) -> str:
        return _fingerprint(value)


__all__ = [
    "PinnedRewardConfig",
    "ExpectedSnapshot",
    "RewardTamperGuard",
    "RunProtection",
    "SECURITY_CONFIG_CHANGED",
    "SECURITY_CONFIRMATION_REQUIRED",
    "SECURITY_EXPECTED_CHANGED",
    "SECURITY_PROTECTED_TARGET",
    "SECURITY_VERIFICATION_CONTROL",
    "SECURITY_VERIFIERS_CHANGED",
    "SecurityFinding",
    "SecurityVerdict",
    "VerificationSecurityPolicy",
]
