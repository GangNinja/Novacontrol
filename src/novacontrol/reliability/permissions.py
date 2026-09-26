"""PermissionManager: one place that decides how dangerous an action is.

Permission checks used to be spread over the callers: the desktop controller
picked scopes, the browser controller picked its own, the tool executor asked
for approval when a tool declared scopes, and a tool that declared nothing was
gated by nothing. This module is the single layer they all describe themselves
to, and it owns three questions:

    how risky is this?              risk_level: LOW | MEDIUM | HIGH | CRITICAL
    may it run without a person?    requires_confirmation, per this build's policy
    what does it need to run?       required_permission, and the effects it has

The vocabulary ALREADY EXISTED — :class:`novacontrol.core.security.RiskLevel`
walks the same four levels the specification names, and
:class:`~novacontrol.tools.metadata.ToolMetadata` already carried a risk and a
permission set. What was missing is the arbitration: a declaration that wins
over a guess, a derivation for the actions nobody declared, and one decision
object for the callers to act on. Nothing here re-implements the approval
gateway: a refusal is still made by the gateway, and this layer says only what
must be asked about and what may not proceed without being asked.

The resolution order is the whole design, and it is honest about its source:

    1. a DECLARATION registered here  — the operator's own statement
    2. the tool's metadata/catalog    — the tool's own statement
    3. a derivation from the ACTION   — the verb's known meaning
    4. LOW and no confirmation        — the default, and it says so

Every decision reports which of the four it came from, so a surprising level
can be traced to whoever supplied it instead of appearing by magic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.tools.metadata import ToolMetadata, highest_risk

#: Risk at or above which a person is asked before the action runs. The same
#: set ``tools.selection`` confirms on, so the two layers cannot disagree about
#: what "sensitive" means.
CONFIRMING_RISK: frozenset[RiskLevel] = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}
)

#: The verbs, and what each one is worth when nobody declared anything. The
#: specification's table, in the same order of severity, with the destructive
#: and external ones marked so their extra rules travel with them.
_ACTION_RISK: tuple[
    tuple[tuple[str, ...], RiskLevel, PermissionScope | None, bool, bool, bool], ...
] = (
    # (markers, risk, permission, destructive, external_side_effect, reversible)
    #
    # Destructive verbs are checked FIRST and on their own: "delete_order"
    # deletes something and must never be read as an order to place, so the
    # word that says "this can destroy data" outranks the word that says
    # "this touches the network".
    (
        ("delete", "remove", "unlink", "wipe", "erase", "format", "truncate", "overwrite"),
        RiskLevel.HIGH,
        PermissionScope.FILESYSTEM_WRITE,
        True,
        False,
        False,
    ),
    (
        ("purchase", "buy", "order", "pay", "checkout", "transfer", "subscribe"),
        RiskLevel.CRITICAL,
        PermissionScope.NETWORK_ACCESS,
        False,
        True,
        False,
    ),
    (
        ("send", "message", "email", "post", "publish", "upload", "notify", "sms", "call"),
        RiskLevel.HIGH,
        PermissionScope.NETWORK_ACCESS,
        False,
        True,
        False,
    ),
    (
        ("modify", "edit", "patch", "refactor", "rewrite", "write", "save", "append", "install"),
        RiskLevel.MEDIUM,
        PermissionScope.FILESYSTEM_WRITE,
        False,
        False,
        True,
    ),
    (
        ("create", "mkdir", "touch", "generate", "new"),
        RiskLevel.LOW,
        PermissionScope.FILESYSTEM_WRITE,
        False,
        False,
        True,
    ),
    (
        (
            "read", "get", "list", "show", "status", "search", "fetch", "browse",
            "open", "launch", "start",
        ),
        RiskLevel.LOW,
        PermissionScope.FILESYSTEM_READ,
        False,
        False,
        True,
    ),
)


@dataclass(frozen=True, slots=True)
class PermissionDeclaration:
    """What a tool/action says about itself, in the specification's terms."""

    risk_level: RiskLevel = RiskLevel.LOW
    required_permission: PermissionScope | None = None
    requires_confirmation: bool = False
    reversible: bool = True
    destructive: bool = False
    external_side_effect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_level": self.risk_level.value,
            "required_permission": (
                self.required_permission.value if self.required_permission else None
            ),
            "requires_confirmation": self.requires_confirmation,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "external_side_effect": self.external_side_effect,
        }

    def merged(self, other: PermissionDeclaration) -> PermissionDeclaration:
        """This declaration with ``other``'s contributions folded in, STRICTLY.

        The same rule :meth:`ToolMetadata.merged` already applies to a tool's own
        fields: the risk is the HIGHEST any source claims, the effects are OR-ed
        (one source calling an action destructive is enough), and reversibility
        is the single field that gets stricter rather than looser.

        This is what stops a general-purpose tool from laundering an action: a
        ``file_manager`` whose metadata is a cheerful MEDIUM cannot make the
        ``delete_file`` it was asked to perform a MEDIUM non-destructive call.
        """
        return PermissionDeclaration(
            risk_level=highest_risk(self.risk_level, other.risk_level),
            required_permission=self.required_permission or other.required_permission,
            requires_confirmation=(
                self.requires_confirmation or other.requires_confirmation
            ),
            reversible=self.reversible and other.reversible,
            destructive=self.destructive or other.destructive,
            external_side_effect=self.external_side_effect or other.external_side_effect,
        )

    @classmethod
    def from_metadata(cls, metadata: ToolMetadata) -> PermissionDeclaration:
        """A declaration read off a tool's own metadata.

        The RISK is the fact and the confirmation flag is a rule applied to it,
        so this does not fold the two together: a tool's explicit
        ``requires_confirmation`` means "always ask me", and whether its risk
        LEVEL is asked about is decided by the policy at the moment of the
        check. Folding them here would make the level's rule impossible to
        relax, which is the wrong place for that decision.
        """
        return cls(
            risk_level=metadata.risk,
            required_permission=metadata.required_permission or _first_scope(metadata),
            requires_confirmation=metadata.requires_confirmation,
            reversible=metadata.reversible,
            destructive=metadata.destructive,
            external_side_effect=metadata.external_side_effect,
        )


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """One action's verdict: what it is, and whether it may proceed now."""

    allow: bool
    risk: RiskLevel
    declaration: PermissionDeclaration
    reason: str
    source: str = "default"
    tool: str = ""
    action: str = ""
    permissions: tuple[PermissionScope, ...] = ()
    requires_confirmation: bool = False

    @property
    def risk_level(self) -> RiskLevel:
        return self.risk

    @property
    def destructive(self) -> bool:
        return self.declaration.destructive

    @property
    def external_side_effect(self) -> bool:
        return self.declaration.external_side_effect

    @property
    def reversible(self) -> bool:
        return self.declaration.reversible

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "risk": self.risk.value,
            "risk_level": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "source": self.source,
            "tool": self.tool,
            "action": self.action,
            "permissions": [scope.value for scope in self.permissions],
            "declaration": self.declaration.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """How careful this build is, and about what."""

    #: Risk at or above which a person is asked.
    confirm_at: frozenset[RiskLevel] = CONFIRMING_RISK
    #: Whether an unapproved destructive/irreversible action is refused outright
    #: (True) or merely marked as needing confirmation (False). True by default:
    #: a delete nobody approved must not be one flag away from running.
    deny_unapproved_destructive: bool = True
    #: Actions that leave the machine are never automatically retried, whatever
    #: the failure kind says.
    never_retry_external: bool = True

    def confirms(self, risk: RiskLevel) -> bool:
        return risk in self.confirm_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirm_at": sorted(level.value for level in self.confirm_at),
            "deny_unapproved_destructive": self.deny_unapproved_destructive,
            "never_retry_external": self.never_retry_external,
        }


@dataclass(slots=True)
class PermissionManager:
    """The centralized risk/permission layer.

    Callers describe the action in whatever terms they have (a tool name, an
    action verb, a metadata object) and get one decision back. Nothing here
    performs the approval — that stays with the approval gateway the rest of
    the system already uses — and nothing here is bypassable by a tool that
    forgot to declare itself: an undeclared action is DERIVED from its verb.
    """

    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    #: The tool catalog to read metadata from, when this build has one.
    catalog: Any | None = None
    _declarations: dict[str, PermissionDeclaration] = field(default_factory=dict, repr=False)

    # -- declarations ----------------------------------------------------------

    def declare(
        self,
        tool_or_action: str,
        declaration: PermissionDeclaration | None = None,
        **fields: Any,
    ) -> PermissionDeclaration:
        """Register how one tool/action behaves, overriding everything derived.

        Accepts either a whole :class:`PermissionDeclaration` or the individual
        fields, so a caller can state only what it knows and leave the rest to
        the policy's defaults.
        """
        name = tool_or_action.strip()
        if not name:
            raise ValueError("A permission declaration needs the tool or action name.")
        stated = declaration or PermissionDeclaration(**fields)
        if declaration is not None and fields:
            stated = replace(declaration, **fields)
        self._declarations[name] = stated
        return stated

    def declared_for(self, name: str) -> PermissionDeclaration | None:
        return self._declarations.get(name.strip())

    def declared(self) -> tuple[str, ...]:
        return tuple(sorted(self._declarations))

    # -- resolution ------------------------------------------------------------

    def resolve(
        self, tool: str = "", *, action: str = ""
    ) -> tuple[PermissionDeclaration, str]:
        """The declaration for this action, and WHICH source supplied the level.

        Every source is consulted and their EFFECTS are folded together, while
        the LEVEL is reported from the source that claimed the highest one. Both
        halves matter: precedence alone (a declaration, then the tool, then the
        verb) is right for deciding what a thing IS, but it lets a tool's
        family-level metadata hide the danger of the single action being
        attempted — and "this deletes a file" must not become MEDIUM because the
        file operation went through a tool that also lists and copies.

        Which is exactly why the verb is read off the ACTION and not off the
        tool's NAME whenever the tool has declared itself. A name is a label,
        not a verb: ``installed_applications`` is a READ that happens to contain
        the letters of "install", and a guess about a name must never outrank
        the tool's own statement about itself. The name is still read when
        nothing declared the tool at all — ``wipe_disk`` is destructive whether
        or not anybody wrote it down.
        """
        sources: list[tuple[str, PermissionDeclaration]] = []
        for key in (tool, action):
            stated = self._declarations.get(key.strip())
            if stated is not None:
                sources.append(("declared", stated))
                break
        metadata = self._metadata_for(tool)
        if metadata is not None:
            sources.append(("metadata", PermissionDeclaration.from_metadata(metadata)))
        derived = _derive(action)
        if derived is None and not sources:
            derived = _derive(tool)
        if derived is not None:
            sources.append(("derived", derived))
        if not sources:
            return PermissionDeclaration(), "default"
        combined = sources[0][1]
        for _source, other in sources[1:]:
            combined = combined.merged(other)
        # ``max`` is stable, so a tie keeps the higher-priority source: an
        # operator's declaration and the verb saying the same thing reports the
        # declaration, which is the one a person can go and look at.
        top = highest_risk(*(item.risk_level for _source, item in sources))
        level_source = next(source for source, item in sources if item.risk_level is top)
        return combined, level_source

    def declaration_for(
        self, tool: str = "", *, action: str = "", parameters: Mapping[str, Any] | None = None
    ) -> PermissionDeclaration:
        """Just the declaration — the shorthand most callers want."""
        del parameters
        return self.resolve(tool, action=action)[0]

    def risk_of(self, tool: str = "", *, action: str = "") -> RiskLevel:
        return self.resolve(tool, action=action)[0].risk_level

    def requires_confirmation(
        self, tool: str = "", *, action: str = "", parameters: Mapping[str, Any] | None = None
    ) -> bool:
        del parameters
        declaration = self.declaration_for(tool, action=action)
        return declaration.requires_confirmation or self.policy.confirms(declaration.risk_level)

    def is_destructive(self, tool: str = "", *, action: str = "") -> bool:
        return self.declaration_for(tool, action=action).destructive

    def is_reversible(self, tool: str = "", *, action: str = "") -> bool:
        return self.declaration_for(tool, action=action).reversible

    def has_external_side_effect(self, tool: str = "", *, action: str = "") -> bool:
        return self.declaration_for(tool, action=action).external_side_effect

    def may_retry(self, tool: str = "", *, action: str = "") -> bool:
        """Whether an automatic retry of this action is acceptable at all."""
        declaration = self.declaration_for(tool, action=action)
        if declaration.destructive or not declaration.reversible:
            return False
        return not (
            self.policy.never_retry_external and declaration.external_side_effect
        )

    # -- the decision ----------------------------------------------------------

    def assess(
        self,
        tool: str = "",
        *,
        action: str = "",
        parameters: Mapping[str, Any] | None = None,
        permissions: Sequence[PermissionScope] | None = None,
    ) -> PermissionDecision:
        """What this action is, before anything is asked about it.

        ``parameters`` is accepted and not yet read: the rules that currently
        exist are about the KIND of action (what it does, and to what), and a
        rule that read a path to decide whether something is inside a workspace
        would be a guess dressed as a judgement. The parameter stays in the
        signature so a caller does not have to change when one is added.
        """
        del parameters
        declaration, source = self.resolve(tool, action=action)
        scopes = tuple(permissions) if permissions else _scopes_for(declaration)
        # Irreversibility asks for a person on its own, whatever the level says:
        # "low risk" is a claim about how likely a mistake is, and this field is
        # a claim about whether one could be undone. A message sent at LOW risk
        # cannot be unsent, so the level alone must not be the whole rule.
        requires = (
            declaration.requires_confirmation
            or self.policy.confirms(declaration.risk_level)
            or not declaration.reversible
        )
        return PermissionDecision(
            allow=True,
            risk=declaration.risk_level,
            declaration=declaration,
            source=source,
            tool=tool,
            action=action,
            permissions=scopes,
            requires_confirmation=requires,
            reason=_assessment_reason(tool, action, declaration, source, requires),
        )

    def check(
        self,
        tool: str = "",
        *,
        action: str = "",
        approved: bool = False,
        parameters: Mapping[str, Any] | None = None,
        permissions: Sequence[PermissionScope] | None = None,
    ) -> PermissionDecision:
        """Whether the action may proceed NOW, given what has been approved.

        A destructive action that nobody approved is refused (see
        :class:`PermissionPolicy`), and a critical one always needs a person:
        both are refusals here rather than at the point of doing the work,
        because \"the caller was supposed to ask\" is not a safety mechanism.
        """
        decision = self.assess(
            tool, action=action, parameters=parameters, permissions=permissions
        )
        if approved or not decision.requires_confirmation:
            return decision
        if self.policy.deny_unapproved_destructive and (
            decision.destructive or not decision.reversible
        ):
            return replace(
                decision,
                allow=False,
                reason=(
                    f"{_label(tool, action)} is {decision.risk.value} risk and cannot be "
                    "undone, so it was not started without an explicit approval."
                ),
            )
        if decision.risk is RiskLevel.CRITICAL:
            return replace(
                decision,
                allow=False,
                reason=(
                    f"{_label(tool, action)} is critical risk and always needs a person's "
                    "approval before it runs."
                ),
            )
        return replace(
            decision,
            allow=False,
            reason=(
                f"{_label(tool, action)} is {decision.risk.value} risk, so it needs "
                "confirmation before it runs."
            ),
        )

    # -- reporting -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "declared": list(self.declared()),
            "levels": [level.value for level in RiskLevel],
        }

    def _metadata_for(self, tool: str) -> ToolMetadata | None:
        if self.catalog is None or not tool.strip():
            return None
        getter = getattr(self.catalog, "get", None)
        if getter is None:
            return None
        found = getter(tool)
        return found if isinstance(found, ToolMetadata) else None


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


def _derive(action: str) -> PermissionDeclaration | None:
    """The declaration an action's VERB implies, if this build knows the verb.

    The first matching row wins, and the rows are ordered so the more dangerous
    reading of an ambiguous verb is the one that applies — see the note on
    :data:`_ACTION_RISK`. The derived declaration states the RISK and the
    effects; whether that risk is asked about is the policy's call, made in
    :meth:`PermissionManager.assess`.
    """
    words = action.strip().lower()
    if not words:
        return None
    for markers, risk, scope, destructive, external, reversible in _ACTION_RISK:
        if any(marker in words for marker in markers):
            return PermissionDeclaration(
                risk_level=risk,
                required_permission=scope,
                reversible=reversible,
                destructive=destructive,
                external_side_effect=external,
            )
    return None


def _first_scope(metadata: ToolMetadata) -> PermissionScope | None:
    """The single scope a sensitivity is "about", from the full declared set."""
    return metadata.permissions[0] if metadata.permissions else None


def _scopes_for(declaration: PermissionDeclaration) -> tuple[PermissionScope, ...]:
    if declaration.required_permission is None:
        return ()
    return (declaration.required_permission,)


def _label(tool: str, action: str) -> str:
    return tool or action or "this action"


def _assessment_reason(
    tool: str,
    action: str,
    declaration: PermissionDeclaration,
    source: str,
    requires_confirmation: bool,
) -> str:
    effects = [
        name
        for name, present in (
            ("destructive", declaration.destructive),
            ("irreversible", not declaration.reversible),
            ("external", declaration.external_side_effect),
        )
        if present
    ]
    detail = f" ({', '.join(effects)})" if effects else ""
    return (
        f"{_label(tool, action)} is {declaration.risk_level.value} risk{detail}, "
        f"declared by {source}; "
        + (
            "a person is asked before it runs."
            if requires_confirmation
            else "no confirmation is required at this level."
        )
    )


__all__ = [
    "CONFIRMING_RISK",
    "PermissionDecision",
    "PermissionDeclaration",
    "PermissionManager",
    "PermissionPolicy",
]
