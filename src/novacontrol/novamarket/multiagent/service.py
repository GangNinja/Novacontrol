"""PI-17 coordinator and public service surface.

The coordinator runs a bounded, concurrent decision cycle over read-only
specialists.  It is intentionally not a second general-purpose agent framework:
it owns financial evidence contracts, financial conflict rules and an advisory
handoff into the existing PI-12 -> NM-08 -> NM-10 workflow.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from novacontrol.novamarket.multiagent.agents import (
    DEFINITION_BY_ID,
    DEFINITIONS,
    agent_for,
    relevant_agents,
)
from novacontrol.novamarket.multiagent.aggregation import (
    REQUIRED_AGENT_IDS,
    TRADING_DECISION_DEFINITION,
    aggregate,
    detect_conflicts,
)
from novacontrol.novamarket.multiagent.models import (
    DEFAULT_CYCLE_TIMEOUT_SECONDS,
    HORIZON_GROUPS,
    MAX_AGENT_TIMEOUT_SECONDS,
    MAX_AGENTS_PER_CYCLE,
    MAX_CONCURRENCY,
    MAX_CYCLE_AGE_DAYS,
    MULTI_AGENT_CONTRACT_VERSION,
    MULTI_AGENT_PHASE,
    READ_CAPABILITIES,
    SUPPORTED_HORIZONS,
    AgentConfiguration,
    AgentHealth,
    AgentMessage,
    AgentRequest,
    AgentResponse,
    AgentRunStatus,
    CycleRequest,
    CycleStatus,
    DecisionCycle,
    EvidenceRecord,
    FinancialAgentRole,
    MessageType,
    MultiAgentNotFoundError,
    MultiAgentResourceError,
    MultiAgentStateError,
    MultiAgentValidationError,
    RecommendationAction,
    canonical_horizon,
    stable_id,
    text,
    utcnow,
)
from novacontrol.novamarket.multiagent.store import MultiAgentStore

COORDINATOR_ID = "multiagent-coordinator"
DIRECTIONAL_ROLES = {
    FinancialAgentRole.TECHNICAL_ANALYSIS,
    FinancialAgentRole.FUNDAMENTAL_ANALYSIS,
    FinancialAgentRole.PREDICTION_INTELLIGENCE,
    FinancialAgentRole.MARKET_REGIME,
    FinancialAgentRole.RL_POLICY_INTELLIGENCE,
}
AGENT_CONTEXT_KEYS: dict[str, tuple[str, ...]] = {
    "fin-market-research": ("market", "news"),
    "fin-technical": ("features", "market"),
    "fin-fundamental": ("fundamentals",),
    "fin-prediction": ("forecasts", "evaluation"),
    "fin-regime": ("regime", "adaptive"),
    "fin-risk": ("risk", "portfolio"),
    "fin-portfolio": ("portfolio",),
    "fin-rl-policy": ("rl", "adaptive", "regime"),
}
PROHIBITED_FINDING_KEYS = {
    "order_id",
    "order_ids",
    "submit_order",
    "submit",
    "execute_order",
    "execution_result",
    "fill_id",
    "ledger_entry",
    "portfolio_mutation",
    "broker_connection",
    "live_trading",
    "autonomy_mode",
    "operator_permission",
    "activate_policy",
    "promote_policy",
}
ALLOWED_AGENT_ACTIONS: dict[FinancialAgentRole, frozenset[RecommendationAction]] = {
    FinancialAgentRole.MARKET_RESEARCH: frozenset({RecommendationAction.ABSTAIN}),
    FinancialAgentRole.TECHNICAL_ANALYSIS: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.ABSTAIN,
        }
    ),
    FinancialAgentRole.FUNDAMENTAL_ANALYSIS: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.ABSTAIN,
        }
    ),
    FinancialAgentRole.PREDICTION_INTELLIGENCE: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.ABSTAIN,
        }
    ),
    FinancialAgentRole.MARKET_REGIME: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.WAIT,
            RecommendationAction.ABSTAIN,
        }
    ),
    FinancialAgentRole.RISK_ANALYSIS: frozenset({RecommendationAction.ABSTAIN}),
    FinancialAgentRole.PORTFOLIO_INTELLIGENCE: frozenset({RecommendationAction.ABSTAIN}),
    FinancialAgentRole.RL_POLICY_INTELLIGENCE: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.WAIT,
            RecommendationAction.NO_ACTION,
            RecommendationAction.ABSTAIN,
        }
    ),
    FinancialAgentRole.TRADING_DECISION: frozenset(
        {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
            RecommendationAction.HOLD,
            RecommendationAction.WAIT,
            RecommendationAction.NO_ACTION,
        }
    ),
}


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_size(value: object, limit: int = 200_000) -> bool:
    try:
        encoded = json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return len(encoded.encode("utf-8")) <= limit


def _find_prohibited(value: object, path: str = "") -> str:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            name = str(key).strip().lower()
            if name in PROHIBITED_FINDING_KEYS:
                return f"{path}.{name}".lstrip(".")
            found = _find_prohibited(nested, f"{path}.{name}")
            if found:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, nested in enumerate(value):
            found = _find_prohibited(nested, f"{path}[{index}]")
            if found:
                return found
    return ""


class MultiAgentIntelligenceService:
    """Bounded orchestration, provenance and advisory aggregation for PI-17."""

    def __init__(
        self,
        store_path: str | Any,
        *,
        context_loader: Callable[[CycleRequest], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_workers: int = MAX_CONCURRENCY,
        cycle_timeout_seconds: int = DEFAULT_CYCLE_TIMEOUT_SECONDS,
        store: MultiAgentStore | None = None,
    ) -> None:
        self._clock = clock or utcnow
        self._store = store or MultiAgentStore(store_path)
        self._context_loader = context_loader
        self.max_workers = max(1, min(int(max_workers), MAX_CONCURRENCY))
        self.cycle_timeout_seconds = max(
            1, min(int(cycle_timeout_seconds), MAX_AGENT_TIMEOUT_SECONDS * 2)
        )
        self._lock = threading.RLock()
        self._running: set[str] = set()
        self._store.ensure_configs(
            [
                AgentConfiguration(
                    agent_id=item.agent_id,
                    enabled=True,
                    timeout_seconds=item.timeout_seconds,
                    provider=item.provider,
                    model_version=item.model_version,
                    allowed_capabilities=item.allowed_capabilities,
                )
                for item in DEFINITIONS
            ]
        )
        self._store.recover_stale_running(older_than_seconds=3_600, now=self._clock())

    # -- registry/configuration ------------------------------------------------
    @property
    def store(self) -> MultiAgentStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    def _config(self, agent_id: str) -> AgentConfiguration:
        config = self._store.config(agent_id)
        if config is None:
            definition = DEFINITION_BY_ID[agent_id]
            config = AgentConfiguration(
                agent_id=agent_id,
                timeout_seconds=definition.timeout_seconds,
                provider=definition.provider,
                model_version=definition.model_version,
                allowed_capabilities=definition.allowed_capabilities,
            )
            self._store.ensure_configs([config])
            config = self._store.config(agent_id) or config
        return config

    def _performance_map(self) -> dict[str, Any]:
        return {item.agent_id: item for item in self._store.performance()}

    def _agent_view(self, agent_id: str) -> dict[str, Any]:
        definition = DEFINITION_BY_ID[agent_id]
        config = self._config(agent_id)
        perf = self._performance_map().get(agent_id)
        health = AgentHealth.DISABLED if not config.enabled else (
            perf.health if perf else AgentHealth.HEALTHY
        )
        return {
            **definition.to_dict(),
            "config_version": str(config.version),
            "enabled": config.enabled,
            "timeout_seconds": config.timeout_seconds,
            "provider": config.provider,
            "model_version": config.model_version,
            "allowed_capabilities": list(config.allowed_capabilities),
            "health": health.value,
            "health_basis": (
                "disabled by configuration"
                if not config.enabled
                else "isolated execution history; not predictive quality"
            ),
            "performance": perf.to_dict() if perf else None,
            "last_execution": perf.last_execution if perf else "",
            "production_authority": False,
            "financial_mutation": False,
        }

    def agents(self) -> dict[str, Any]:
        views = [self._agent_view(item.agent_id) for item in DEFINITIONS]
        decision = {
            **TRADING_DECISION_DEFINITION.to_dict(),
            "enabled": True,
            "health": AgentHealth.HEALTHY.value,
            "performance": None,
            "last_execution": "",
            "production_authority": False,
            "financial_mutation": False,
            "not_a_parallel_specialist": True,
        }
        return {
            "phase": MULTI_AGENT_PHASE,
            "contract_version": MULTI_AGENT_CONTRACT_VERSION,
            "agents": [*views, decision],
            "count": len(views) + 1,
            "coordination_agent_id": COORDINATOR_ID,
            "advisory_only": True,
        }

    def agent(self, agent_id: str) -> dict[str, Any]:
        if agent_id == TRADING_DECISION_DEFINITION.agent_id:
            return {
                **TRADING_DECISION_DEFINITION.to_dict(),
                "enabled": True,
                "health": AgentHealth.HEALTHY.value,
                "production_authority": False,
                "financial_mutation": False,
            }
        if agent_id not in DEFINITION_BY_ID:
            raise MultiAgentNotFoundError(f"unknown multi-agent specialist {agent_id!r}")
        return {
            **self._agent_view(agent_id),
            "config_audit": self._store.config_audit(agent_id, 20),
            "input_contract": DEFINITION_BY_ID[agent_id].input_contract,
            "output_contract": DEFINITION_BY_ID[agent_id].output_contract,
        }

    def update_config(
        self, agent_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if agent_id not in DEFINITION_BY_ID:
            raise MultiAgentNotFoundError(f"unknown multi-agent specialist {agent_id!r}")
        definition = DEFINITION_BY_ID[agent_id]
        if text(payload.get("role")) and text(payload.get("role")) != definition.role.value:
            raise MultiAgentValidationError("an agent's role cannot be changed by configuration")
        if "required" in payload and bool(payload["required"]) != definition.required:
            raise MultiAgentValidationError("required-agent status is fixed by the task type")
        provider = text(payload.get("provider"), definition.provider)
        if provider != "deterministic-local":
            raise MultiAgentValidationError(
                "only the built-in deterministic provider is available; "
                "no new model dependency is installed"
            )
        requested = payload.get("allowed_capabilities")
        if requested is None:
            capabilities = definition.allowed_capabilities
        else:
            if isinstance(requested, str):
                requested = [requested]
            if not isinstance(requested, Sequence):
                raise MultiAgentValidationError("allowed_capabilities must be a list")
            capabilities = tuple(str(item).strip().lower() for item in requested)
            forbidden = [item for item in capabilities if item not in READ_CAPABILITIES]
            if forbidden:
                raise MultiAgentValidationError(
                    "unknown or non-read capabilities: " + ", ".join(forbidden)
                )
            outside = [item for item in capabilities if item not in definition.allowed_capabilities]
            if outside:
                raise MultiAgentValidationError(
                    "capabilities exceed the agent's declared permission boundary: "
                    + ", ".join(outside)
                )
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MultiAgentValidationError("enabled must be a boolean")
        timeout = payload.get("timeout_seconds", definition.timeout_seconds)
        if isinstance(timeout, bool):
            raise MultiAgentValidationError("timeout_seconds must be an integer")
        try:
            timeout_int = int(timeout)
        except (TypeError, ValueError) as exc:
            raise MultiAgentValidationError("timeout_seconds must be an integer") from exc
        if not 1 <= timeout_int <= MAX_AGENT_TIMEOUT_SECONDS:
            raise MultiAgentValidationError(
                f"timeout_seconds must be within [1, {MAX_AGENT_TIMEOUT_SECONDS}]"
            )
        config = AgentConfiguration(
            agent_id=agent_id,
            enabled=enabled,
            timeout_seconds=timeout_int,
            provider=provider,
            model_version=text(payload.get("model_version"), definition.model_version),
            allowed_capabilities=capabilities,
            version=1,
            updated_at=self._clock(),
        )
        saved = self._store.save_config(
            config, audit_id=stable_id("maaud_", agent_id, uuid.uuid4().hex)
        )
        return {
            **self.agent(agent_id),
            "configuration": saved.to_dict(),
            "configuration_version": saved.version,
            "financial_mutation": False,
        }

    def health(self) -> dict[str, Any]:
        configs = self._store.configs()
        perf = self._performance_map()
        unhealthy = [
            item.agent_id
            for item in perf.values()
            if item.health in {AgentHealth.DEGRADED, AgentHealth.UNAVAILABLE}
            and configs.get(item.agent_id, AgentConfiguration(item.agent_id)).enabled
        ]
        disabled = [key for key, item in configs.items() if not item.enabled]
        return {
            "phase": MULTI_AGENT_PHASE,
            "ok": not unhealthy,
            "health": "degraded" if unhealthy else "healthy",
            "unhealthy_agents": unhealthy,
            "disabled_agents": disabled,
            "store": self._store.health(),
            "active_cycles": len(self._running),
            "authority_health": {
                "nm08": "untouched external authority",
                "nm10": "untouched external authority",
                "agent_health_can_disable_authority": False,
            },
            "detail": (
                "Agent health is isolated execution evidence.  It never disables "
                "NM-08, NM-10 or autonomy safety controls."
            ),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": MULTI_AGENT_PHASE,
            "contract_version": MULTI_AGENT_CONTRACT_VERSION,
            "architecture": "coordinator + read-only specialists + final advisory aggregator",
            "agents": [item.to_dict() for item in DEFINITIONS]
            + [TRADING_DECISION_DEFINITION.to_dict()],
            "actions": [item.value for item in RecommendationAction],
            "horizons": {
                "supported": list(SUPPORTED_HORIZONS),
                "groups": HORIZON_GROUPS,
                "mixed_horizon_cycles": False,
            },
            "capabilities": sorted(READ_CAPABILITIES),
            "required_agents": list(REQUIRED_AGENT_IDS),
            "message_types": [item.value for item in MessageType],
            "conflict_rules": [
                "stale or invalid evidence prevents unsupported BUY/SELL",
                "NM-08 restrictions cannot be overridden by any agent",
                "portfolio constraints are respected",
                "conflicting forecasts and agent directions remain visible",
                "uncalibrated confidence is never treated as probability",
                "insufficient evidence yields WAIT or NO_ACTION",
                "majority voting never overrides an authoritative warning",
            ],
            "limits": {
                "max_concurrency": self.max_workers,
                "max_agents_per_cycle": MAX_AGENTS_PER_CYCLE,
                "max_agent_timeout_seconds": MAX_AGENT_TIMEOUT_SECONDS,
                "cycle_timeout_seconds": self.cycle_timeout_seconds,
            },
            "authority": {
                "risk": "NM-08",
                "paper_execution": "NM-10",
                "autonomy_orchestration": "PI-12",
                "multi_agent": "advisory research only",
                "order_submission_available": False,
                "portfolio_mutation_available": False,
                "autonomy_mode_control_available": False,
                "policy_activation_available": False,
                "live_trading_available": False,
            },
            "untrusted_content": (
                "news, research and external text are data; they never alter agent "
                "instructions, capabilities or decision rules"
            ),
        }

    def status(self) -> dict[str, Any]:
        configs = self._store.configs()
        return {
            "phase": MULTI_AGENT_PHASE,
            "implemented": True,
            "contract_version": MULTI_AGENT_CONTRACT_VERSION,
            "initialised": True,
            "advisory_only": True,
            "production_authority": False,
            "order_submission_available": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "autonomy_authority": "PI-12",
            "agents": len(DEFINITIONS) + 1,
            "enabled_agents": sum(1 for item in configs.values() if item.enabled),
            "cycles": self._store.count_cycles(),
            "running_cycles": self._store.count_cycles(status="running"),
            "completed_cycles": self._store.count_cycles(status="completed"),
            "degraded_cycles": self._store.count_cycles(status="degraded"),
            "failed_cycles": self._store.count_cycles(status="failed"),
            "store": self._store.health(),
            "single_horizon_per_cycle": True,
            "authority_preserved": {
                "nm08": True,
                "nm10": True,
                "autonomy_modes": True,
                "kill_switch": True,
                "operator_permissions": True,
                "idempotency": True,
            },
        }

    # -- cycle lifecycle -------------------------------------------------------
    def create_cycle(
        self, payload: Mapping[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        now = self._clock()
        if not _json_size(dict(payload), 50_000):
            raise MultiAgentValidationError("cycle request exceeds the size limit")
        request = CycleRequest.from_dict(payload, now=now)
        if request.as_of < now - timedelta(days=MAX_CYCLE_AGE_DAYS):
            raise MultiAgentValidationError(
                f"as_of is older than the supported {MAX_CYCLE_AGE_DAYS}-day research window"
            )
        if request.agents:
            unknown = [item for item in request.agents if item not in DEFINITION_BY_ID]
            if unknown:
                raise MultiAgentValidationError(
                    "unknown financial agents: " + ", ".join(unknown)
                )
        key = text(idempotency_key or payload.get("idempotency_key"))
        if key:
            seed = stable_id(
                "mac_",
                key,
                request.instrument_id,
                request.exchange,
                request.horizon,
                request.as_of.isoformat(),
            )
        else:
            seed = "mac_" + uuid.uuid4().hex
        existing = self._store.cycle(seed)
        if existing is not None:
            return {
                "created": False,
                "idempotent_reuse": True,
                "cycle": existing.to_dict(),
            }
        if request.agents:
            selected = request.agents
        else:
            hint: dict[str, Any] = {}
            if payload.get("include_research"):
                hint["news"] = True
            if payload.get("include_rl"):
                hint["rl"] = {"policies": [{}]}
            selected = relevant_agents(
                horizon=request.horizon, available_context=hint
            )
        # Pinning specialists is additive.  A caller can never remove the
        # required technical/prediction/regime/risk/portfolio evidence and turn
        # a partial research run into an actionable recommendation.
        assigned = tuple(dict.fromkeys([*REQUIRED_AGENT_IDS, *selected]))
        if not assigned:
            raise MultiAgentValidationError("no relevant agents were selected")
        if len(assigned) > MAX_AGENTS_PER_CYCLE:
            raise MultiAgentResourceError(
                f"at most {MAX_AGENTS_PER_CYCLE} agents can run in one cycle"
            )
        assigned = tuple(dict.fromkeys(assigned))
        required = tuple(item for item in assigned if item in REQUIRED_AGENT_IDS)
        correlation = request.correlation_id or seed
        cycle = DecisionCycle(
            cycle_id=seed,
            instrument_id=request.instrument_id,
            exchange=request.exchange,
            horizon=request.horizon,
            status=CycleStatus.PENDING,
            as_of=request.as_of,
            requested_agents=request.agents,
            assigned_agents=assigned,
            required_agents=required,
            correlation_id=correlation,
            created_at=now,
            metadata={
                "portfolio_id": request.portfolio_id,
                "advisory_only": True,
                "production_authority": False,
                "idempotency_key": key,
                "requested_payload_keys": sorted(request.payload),
            },
        )
        self._store.save_cycle(cycle, request_payload=request.to_dict())
        return {
            "created": True,
            "idempotent_reuse": False,
            "cycle": cycle.to_dict(),
        }

    def run_cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._store.cycle(cycle_id)
        if cycle is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        if cycle.status in {
            CycleStatus.COMPLETED,
            CycleStatus.DEGRADED,
            CycleStatus.FAILED,
        }:
            existing_decision = self._store.decision(cycle_id)
            return {
                "cycle": cycle.to_dict(),
                "decision": existing_decision.to_dict() if existing_decision else None,
                "idempotent_reuse": True,
            }
        if cycle.status is CycleStatus.CANCELLED:
            raise MultiAgentStateError("a cancelled decision cycle cannot run")
        with self._lock:
            if cycle_id in self._running:
                raise MultiAgentStateError("this decision cycle is already running")
            self._running.add(cycle_id)
        try:
            if not self._store.claim_cycle(cycle_id, started_at=self._clock()):
                current = self._store.cycle(cycle_id)
                if current and current.status is CycleStatus.CANCELLED:
                    raise MultiAgentStateError("the decision cycle was cancelled")
                return {
                    "cycle": current.to_dict() if current else cycle.to_dict(),
                    "decision": None,
                    "idempotent_reuse": True,
                    "detail": "another worker already claimed this cycle",
                }
            running = replace(
                cycle,
                status=CycleStatus.RUNNING,
                started_at=self._clock(),
                current_agent="",
                assigned_agents=tuple(
                    dict.fromkeys([*REQUIRED_AGENT_IDS, *cycle.assigned_agents])
                ),
                required_agents=REQUIRED_AGENT_IDS,
            )
            return self._execute_cycle(running)
        finally:
            with self._lock:
                self._running.discard(cycle_id)

    def _load_context(self, request: CycleRequest) -> Mapping[str, Any]:
        if self._context_loader is None:
            return {}
        try:
            payload = self._context_loader(request)
        except Exception as exc:  # noqa: BLE001 - cycle must degrade safely
            raise MultiAgentStateError(
                f"read-only production context could not be loaded: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise MultiAgentValidationError("context loader must return an object")
        return payload

    def _slice_context(
        self,
        agent_id: str,
        context: Mapping[str, Any],
        config: AgentConfiguration,
    ) -> Mapping[str, Any]:
        required_keys = AGENT_CONTEXT_KEYS[agent_id]
        capability_for_key = {
            "market": "market.read",
            "news": "news.read",
            "features": "features.read",
            "fundamentals": "fundamentals.read",
            "forecasts": "forecasts.read",
            "evaluation": "evaluation.read",
            "regime": "regime.read",
            "risk": "risk.read",
            "portfolio": "portfolio.read",
            "rl": "rl.read",
            "adaptive": "adaptive.read",
        }
        selected: dict[str, Any] = {}
        for key in required_keys:
            capability = capability_for_key[key]
            if capability not in config.allowed_capabilities:
                raise PermissionError(
                    f"{agent_id} lacks required read capability {capability}"
                )
            if key in context:
                selected[key] = context[key]
        if "market" in selected:
            market = dict(_as_mapping(selected["market"]))
            if "news" in context and "news.read" in config.allowed_capabilities:
                market["news"] = context["news"]
            if "events" in context and "news.read" in config.allowed_capabilities:
                market["events"] = context["events"]
            selected["market"] = market
        selected["cycle"] = {
            "cycle_id": "",
            "advisory_only": True,
            "production_authority": False,
        }
        return selected

    def _task_message(
        self,
        cycle: DecisionCycle,
        agent_id: str,
        *,
        as_of: datetime,
    ) -> AgentMessage:
        recipient = DEFINITION_BY_ID[agent_id]
        return AgentMessage(
            message_id=stable_id("mam_", cycle.cycle_id, agent_id, as_of.isoformat()),
            cycle_id=cycle.cycle_id,
            sender_agent_id=COORDINATOR_ID,
            recipient_agent_id=agent_id,
            task_id=stable_id("mat_", cycle.cycle_id, agent_id),
            message_type=MessageType.TASK_ASSIGNMENT,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            payload={
                "horizon": cycle.horizon,
                "as_of": as_of.isoformat(),
                "input_contract": recipient.input_contract,
                "advisory_only": True,
            },
            created_at=as_of,
            expires_at=as_of
            + timedelta(seconds=self._config(agent_id).timeout_seconds + 5),
            correlation_id=cycle.correlation_id,
            config_version=str(self._config(agent_id).version),
            source_references=(recipient.input_contract,),
        )

    def validate_message(
        self, message: AgentMessage, *, expected_cycle_id: str = ""
    ) -> None:
        known = {
            COORDINATOR_ID,
            TRADING_DECISION_DEFINITION.agent_id,
            *DEFINITION_BY_ID,
        }
        if message.sender_agent_id not in known:
            raise MultiAgentValidationError("message sender is not an authorized agent")
        if message.recipient_agent_id not in known:
            raise MultiAgentValidationError("message recipient is not an authorized agent")
        if expected_cycle_id and message.cycle_id != expected_cycle_id:
            raise MultiAgentValidationError("cross-cycle message contamination rejected")
        if message.created_at > message.expires_at:
            raise MultiAgentValidationError("message expiration precedes creation")
        if message.expires_at < self._clock():
            raise MultiAgentValidationError("message has expired")
        if not _json_size(dict(message.payload), 100_000):
            raise MultiAgentValidationError("message payload exceeds the size limit")
        if not message.message_id.startswith("mam_"):
            raise MultiAgentValidationError("message identity is not coordinator-issued")
        cycle = self._store.cycle(message.cycle_id)
        if cycle is None:
            raise MultiAgentValidationError("message targets an unknown decision cycle")
        if message.created_at < cycle.created_at - timedelta(seconds=5):
            raise MultiAgentValidationError("message predates its decision cycle")

    def _validate_response(
        self,
        response: AgentResponse,
        *,
        request: AgentRequest,
        definition: Any,
        as_of: datetime,
    ) -> AgentResponse:
        if not isinstance(response, AgentResponse):
            raise MultiAgentValidationError("agent returned an unsupported response type")
        if response.cycle_id != request.message.cycle_id:
            raise MultiAgentValidationError("agent response belongs to another cycle")
        if response.agent_id != definition.agent_id:
            raise MultiAgentValidationError("agent response identity does not match its task")
        if response.role is not definition.role:
            raise MultiAgentValidationError("agent response role does not match its registration")
        if response.status not in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.INSUFFICIENT_EVIDENCE,
            AgentRunStatus.INVALID,
            AgentRunStatus.FAILED,
        }:
            raise MultiAgentValidationError("agent returned a non-terminal execution status")
        if response.action not in ALLOWED_AGENT_ACTIONS[definition.role]:
            raise MultiAgentValidationError(
                f"{definition.agent_id} returned action "
                f"{response.action.value!r} outside its role contract"
            )
        if response.action in {RecommendationAction.BUY, RecommendationAction.SELL}:
            if not response.evidence:
                raise MultiAgentValidationError(
                    "a directional recommendation requires retained evidence"
                )
            if not response.supporting and response.action is RecommendationAction.BUY:
                raise MultiAgentValidationError(
                    "an upside recommendation requires supporting findings"
                )
            if not response.contradicting and response.action is RecommendationAction.SELL:
                raise MultiAgentValidationError(
                    "a downside recommendation requires supporting findings"
                )
        if not _json_size(response.to_dict(), 300_000):
            raise MultiAgentValidationError("agent response exceeds the payload limit")
        prohibited = _find_prohibited(response.findings)
        if prohibited:
            raise MultiAgentValidationError(
                f"agent output attempted a financial mutation/authority field: {prohibited}"
            )
        for item in response.evidence:
            if item.cycle_id != request.message.cycle_id:
                raise MultiAgentValidationError("evidence belongs to another decision cycle")
            if item.agent_id != definition.agent_id:
                raise MultiAgentValidationError("evidence agent identity does not match")
            if item.instrument_id and item.instrument_id != request.message.instrument_id:
                raise MultiAgentValidationError("evidence instrument does not match the cycle")
            for stamp_name, stamp in (
                ("available_at", item.available_at),
                ("published_at", item.published_at),
                ("original_at", item.original_at),
            ):
                if stamp is not None and stamp > request.as_of + timedelta(seconds=1):
                    raise MultiAgentValidationError(
                        f"evidence {stamp_name} is after the cycle cutoff (future leakage)"
                    )
        return response

    def _error_response(
        self,
        request: AgentRequest,
        definition: Any,
        *,
        status: AgentRunStatus,
        started: datetime,
        error: str,
    ) -> AgentResponse:
        finished = self._clock()
        return AgentResponse(
            message_id=request.message.message_id,
            task_id=request.message.task_id,
            cycle_id=request.message.cycle_id,
            agent_id=definition.agent_id,
            role=definition.role,
            status=status,
            error=error[:2_000],
            latency_ms=max(0.0, (finished - started).total_seconds() * 1000.0),
            config_version=request.message.config_version,
            model_version=definition.model_version,
            started_at=started,
            finished_at=finished,
        )

    def _execute_agent(
        self,
        definition: Any,
        request: AgentRequest,
        config: AgentConfiguration,
    ) -> AgentResponse:
        started = self._clock()
        try:
            response = agent_for(definition.agent_id).analyze(request, definition)
            return self._validate_response(
                response, request=request, definition=definition, as_of=request.as_of
            )
        except PermissionError as exc:
            return self._error_response(
                request,
                definition,
                status=AgentRunStatus.FAILED,
                started=started,
                error=f"permission denied: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - failures are structured evidence
            status = (
                AgentRunStatus.INVALID
                if isinstance(exc, (MultiAgentValidationError, MultiAgentResourceError))
                else AgentRunStatus.FAILED
            )
            return self._error_response(
                request,
                definition,
                status=status,
                started=started,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _execute_cycle(self, cycle: DecisionCycle) -> dict[str, Any]:
        request_model = CycleRequest(
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            horizon=cycle.horizon,
            as_of=cycle.as_of,
            portfolio_id=str(cycle.metadata.get("portfolio_id") or ""),
            correlation_id=cycle.correlation_id,
            agents=cycle.assigned_agents,
            payload=dict(cycle.metadata),
        )
        context: Mapping[str, Any] = {}
        context_error = ""
        try:
            context = self._load_context(request_model)
        except Exception as exc:  # noqa: BLE001 - safe degraded cycle
            context_error = f"{type(exc).__name__}: {exc}"
        responses: list[AgentResponse] = []
        futures: dict[Future[AgentResponse], tuple[Any, datetime, AgentConfiguration]] = {}
        started_at = self._clock()
        deadline = started_at + timedelta(seconds=self.cycle_timeout_seconds)
        executor = ThreadPoolExecutor(
            max_workers=min(self.max_workers, max(1, len(cycle.assigned_agents))),
            thread_name_prefix="pi17-agent",
        )
        try:
            for agent_id in cycle.assigned_agents:
                definition = DEFINITION_BY_ID[agent_id]
                config = self._config(agent_id)
                message = self._task_message(cycle, agent_id, as_of=started_at)
                try:
                    self.validate_message(message, expected_cycle_id=cycle.cycle_id)
                    if not self._store.has_message(message.message_id):
                        self._store.save_message(message)
                except Exception as exc:  # noqa: BLE001
                    responses.append(
                        self._error_response(
                            AgentRequest(
                                message=message,
                                horizon=cycle.horizon,
                                as_of=cycle.as_of,
                                context={},
                            ),
                            definition,
                            status=AgentRunStatus.INVALID,
                            started=started_at,
                            error=f"message contract rejected: {exc}",
                        )
                    )
                    continue
                if not config.enabled:
                    responses.append(
                        self._error_response(
                            AgentRequest(
                                message=message,
                                horizon=cycle.horizon,
                                as_of=cycle.as_of,
                                context={},
                            ),
                            definition,
                            status=AgentRunStatus.SKIPPED,
                            started=started_at,
                            error="agent is disabled by configuration",
                        )
                    )
                    continue
                try:
                    sliced = dict(self._slice_context(agent_id, context, config))
                except PermissionError as exc:
                    responses.append(
                        self._error_response(
                            AgentRequest(
                                message=message,
                                horizon=cycle.horizon,
                                as_of=cycle.as_of,
                                context={},
                            ),
                            definition,
                            status=AgentRunStatus.FAILED,
                            started=started_at,
                            error=f"permission denied: {exc}",
                        )
                    )
                    continue
                sliced["cycle"] = {
                    "cycle_id": cycle.cycle_id,
                    "advisory_only": True,
                    "production_authority": False,
                    "correlation_id": cycle.correlation_id,
                }
                agent_request = AgentRequest(
                    message=message,
                    horizon=cycle.horizon,
                    as_of=cycle.as_of,
                    context=sliced,
                )
                future = executor.submit(
                    self._execute_agent, definition, agent_request, config
                )
                futures[future] = (definition, started_at, config)
            for future, (definition, agent_started, config) in list(futures.items()):
                now = self._clock()
                remaining = min(
                    (
                        agent_started
                        + timedelta(seconds=config.timeout_seconds)
                        - now
                    ).total_seconds(),
                    (deadline - now).total_seconds(),
                )
                try:
                    response = future.result(timeout=max(0.001, remaining))
                except TimeoutError:
                    future.cancel()
                    message = self._task_message(cycle, definition.agent_id, as_of=started_at)
                    request = AgentRequest(
                        message=message,
                        horizon=cycle.horizon,
                        as_of=cycle.as_of,
                        context={},
                    )
                    response = self._error_response(
                        request,
                        definition,
                        status=AgentRunStatus.TIMEOUT,
                        started=agent_started,
                        error=(
                            f"agent exceeded its {config.timeout_seconds}s execution timeout"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    message = self._task_message(cycle, definition.agent_id, as_of=started_at)
                    response = self._error_response(
                        AgentRequest(
                            message=message,
                            horizon=cycle.horizon,
                            as_of=cycle.as_of,
                            context={},
                        ),
                        definition,
                        status=AgentRunStatus.FAILED,
                        started=agent_started,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                responses.append(response)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        current = self._store.cycle(cycle.cycle_id)
        if current and current.status is CycleStatus.CANCELLED:
            for response in responses:
                try:
                    self._store.save_run(response)
                except MultiAgentResourceError:
                    break
            return {
                "cycle": current.to_dict(),
                "decision": None,
                "cancelled": True,
                "idempotent_reuse": False,
            }
        evidence: list[EvidenceRecord] = []
        seen_evidence: set[str] = set()
        for response in responses:
            with contextlib.suppress(MultiAgentResourceError):
                self._store.save_run(response)
            for item in response.evidence:
                if item.evidence_id in seen_evidence:
                    continue
                seen_evidence.add(item.evidence_id)
                evidence.append(item)
                with contextlib.suppress(MultiAgentResourceError):
                    self._store.save_evidence(item)
            self._save_evidence_share(cycle, response)
            if response.status in {
                AgentRunStatus.FAILED,
                AgentRunStatus.TIMEOUT,
                AgentRunStatus.INVALID,
            }:
                self._save_notification(cycle, response)
        conflicts = detect_conflicts(
            cycle_id=cycle.cycle_id,
            responses=responses,
            evidence=evidence,
            required_agents=cycle.required_agents,
            horizon=cycle.horizon,
        )
        self._store.save_conflicts(cycle.cycle_id, conflicts)
        failed_required = [
            item.agent_id
            for item in responses
            if item.agent_id in cycle.required_agents
            and item.status
            in {
                AgentRunStatus.FAILED,
                AgentRunStatus.TIMEOUT,
                AgentRunStatus.INVALID,
                AgentRunStatus.SKIPPED,
            }
        ]
        degraded = bool(
            failed_required
            or context_error
            or any(
                item.status
                in {
                    AgentRunStatus.FAILED,
                    AgentRunStatus.TIMEOUT,
                    AgentRunStatus.INVALID,
                    AgentRunStatus.SKIPPED,
                    AgentRunStatus.INSUFFICIENT_EVIDENCE,
                }
                for item in responses
                if item.agent_id in cycle.required_agents
            )
            or any(
                item.status
                in {
                    AgentRunStatus.FAILED,
                    AgentRunStatus.TIMEOUT,
                    AgentRunStatus.INVALID,
                }
                for item in responses
                if item.agent_id not in cycle.required_agents
            )
        )
        decision = aggregate(
            cycle_id=cycle.cycle_id,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            horizon=cycle.horizon,
            responses=responses,
            evidence=evidence,
            required_agents=cycle.required_agents,
            conflicts=conflicts,
            reliability=self._performance_map(),
            degraded=degraded,
            decided_at=self._clock(),
        )
        if not any(item.usable for item in responses) or context_error:
            action = RecommendationAction.NO_ACTION
            no_action_reason = (
                "context loading failed"
                if context_error
                else "no specialist produced usable evidence"
            )
            decision = replace(
                decision,
                action=action,
                explanation=(
                    f"NO_ACTION: {no_action_reason}. "
                    "The cycle is retained for diagnosis. NM-08 and NM-10 were not called."
                ),
                degraded=True,
            )
        for conflict in conflicts:
            self._save_conflict_notification(cycle, conflict)
        finished = self._clock()
        status = (
            CycleStatus.FAILED
            if not any(item.usable for item in responses) or (
                context_error and not responses
            )
            else CycleStatus.DEGRADED
            if degraded
            else CycleStatus.COMPLETED
        )
        final_cycle = replace(
            cycle,
            status=status,
            finished_at=finished,
            current_agent="",
            decision_id=decision.decision_id,
            responses=tuple(responses),
            conflicts=tuple(conflicts),
            degraded=degraded,
            error=context_error
            or (
                "required agent failure: " + ", ".join(failed_required)
                if failed_required
                else ""
            ),
        )
        saved = self._store.finish_cycle(
            final_cycle,
            responses=responses,
            conflicts=conflicts,
            decision=decision,
            error=final_cycle.error,
        )
        if not saved:
            current = self._store.cycle(cycle.cycle_id)
            existing_decision = self._store.decision(cycle.cycle_id)
            return {
                "cycle": current.to_dict() if current else final_cycle.to_dict(),
                "decision": existing_decision.to_dict() if existing_decision else None,
                "cancelled": True,
                "idempotent_reuse": False,
            }
        return {
            "cycle": final_cycle.to_dict(),
            "decision": decision.to_dict(),
            "cancelled": False,
            "idempotent_reuse": False,
        }

    def _save_evidence_share(self, cycle: DecisionCycle, response: AgentResponse) -> None:
        if not response.evidence:
            return
        message = AgentMessage(
            message_id=stable_id(
                "mam_", cycle.cycle_id, response.agent_id, "evidence", response.message_id
            ),
            cycle_id=cycle.cycle_id,
            sender_agent_id=response.agent_id,
            recipient_agent_id=TRADING_DECISION_DEFINITION.agent_id,
            task_id=response.task_id,
            message_type=MessageType.EVIDENCE_SHARE,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            payload={
                "evidence_ids": [item.evidence_id for item in response.evidence],
                "count": len(response.evidence),
                "summary": "shared evidence provenance",
            },
            created_at=response.finished_at,
            expires_at=response.finished_at + timedelta(minutes=5),
            correlation_id=cycle.correlation_id,
            config_version=response.config_version,
            source_references=response.source_references,
        )
        try:
            self.validate_message(message, expected_cycle_id=cycle.cycle_id)
            if not self._store.has_message(message.message_id):
                self._store.save_message(message)
        except (MultiAgentValidationError, MultiAgentResourceError):
            return

    def _save_notification(self, cycle: DecisionCycle, response: AgentResponse) -> None:
        message_type = (
            MessageType.TIMEOUT_NOTIFICATION
            if response.status is AgentRunStatus.TIMEOUT
            else MessageType.FAILURE_NOTIFICATION
        )
        message = AgentMessage(
            message_id=stable_id(
                "mam_", cycle.cycle_id, response.agent_id, message_type.value
            ),
            cycle_id=cycle.cycle_id,
            sender_agent_id=response.agent_id,
            recipient_agent_id=COORDINATOR_ID,
            task_id=response.task_id,
            message_type=message_type,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            payload={"status": response.status.value, "error": response.error},
            created_at=response.finished_at,
            expires_at=response.finished_at + timedelta(minutes=5),
            correlation_id=cycle.correlation_id,
            config_version=response.config_version,
        )
        try:
            self.validate_message(message, expected_cycle_id=cycle.cycle_id)
            if not self._store.has_message(message.message_id):
                self._store.save_message(message)
        except (MultiAgentValidationError, MultiAgentResourceError):
            return

    def _save_conflict_notification(self, cycle: DecisionCycle, conflict: Any) -> None:
        message = AgentMessage(
            message_id=stable_id("mam_", cycle.cycle_id, "conflict", conflict.conflict_id),
            cycle_id=cycle.cycle_id,
            sender_agent_id=COORDINATOR_ID,
            recipient_agent_id=TRADING_DECISION_DEFINITION.agent_id,
            task_id=stable_id("mat_", cycle.cycle_id, "conflicts"),
            message_type=MessageType.CONFLICT_NOTIFICATION,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            payload={
                "conflict_id": conflict.conflict_id,
                "kind": conflict.kind.value,
                "severity": conflict.severity.value,
                "preserved": conflict.preserved,
            },
            created_at=self._clock(),
            expires_at=self._clock() + timedelta(minutes=5),
            correlation_id=cycle.correlation_id,
        )
        try:
            self.validate_message(message, expected_cycle_id=cycle.cycle_id)
            if not self._store.has_message(message.message_id):
                self._store.save_message(message)
        except (MultiAgentValidationError, MultiAgentResourceError):
            return

    def cancel_cycle(
        self, cycle_id: str, *, reason: str = "cancelled by operator"
    ) -> dict[str, Any]:
        cycle = self._store.cycle(cycle_id)
        if cycle is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        if not self._store.cancel_cycle(cycle_id, reason=reason):
            if cycle.status in {
                CycleStatus.COMPLETED,
                CycleStatus.DEGRADED,
                CycleStatus.FAILED,
            }:
                raise MultiAgentStateError("a completed research cycle cannot be cancelled")
            raise MultiAgentStateError("this decision cycle cannot be cancelled")
        current = self._store.cycle(cycle_id)
        return {"cancelled": True, "cycle": current.to_dict() if current else None}

    # -- reads -----------------------------------------------------------------
    def cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._store.cycle(cycle_id)
        if cycle is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        decision = self._store.decision(cycle_id)
        return {
            **cycle.to_dict(),
            "decision": decision.to_dict() if decision else None,
            "events": self._store.events(cycle_id),
            "advisory_only": True,
            "production_authority": False,
        }

    def cycles(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        items = self._store.cycles(
            instrument_id=instrument_id,
            status=status,
            limit=safe_limit,
            offset=safe_offset,
        )
        return {
            "items": [item.to_dict() for item in items],
            "count": len(items),
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": len(items) == safe_limit,
            "advisory_only": True,
        }

    def runs(self, cycle_id: str) -> dict[str, Any]:
        if self._store.cycle(cycle_id) is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        runs = self._store.runs(cycle_id)
        return {
            "cycle_id": cycle_id,
            "agents": [item.to_dict() for item in runs],
            "messages": self._store.messages(cycle_id, limit=500),
            "count": len(runs),
        }

    def evidence(self, cycle_id: str, *, agent_id: str = "") -> dict[str, Any]:
        if self._store.cycle(cycle_id) is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        items = self._store.evidence(cycle_id, agent_id=agent_id)
        return {
            "cycle_id": cycle_id,
            "items": [item.to_dict() for item in items],
            "count": len(items),
            "kinds": sorted({item.kind.value for item in items}),
            "freshness": sorted({item.freshness.value for item in items}),
            "validation": sorted({item.validation.value for item in items}),
        }

    def conflicts(self, cycle_id: str) -> dict[str, Any]:
        if self._store.cycle(cycle_id) is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        items = self._store.conflicts(cycle_id)
        return {
            "cycle_id": cycle_id,
            "items": [item.to_dict() for item in items],
            "count": len(items),
            "unresolved": sum(1 for item in items if not item.resolved),
        }

    def decision(self, cycle_id: str) -> dict[str, Any]:
        item = self._store.decision(cycle_id)
        if item is None:
            raise MultiAgentNotFoundError(
                f"decision cycle {cycle_id!r} has no completed recommendation"
            )
        return item.to_dict()

    def history(
        self,
        *,
        instrument_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        decisions = self._store.decisions(
            instrument_id=instrument_id, limit=safe_limit, offset=safe_offset
        )
        cycles = {
            item.cycle_id: item
            for item in self._store.cycles(
                instrument_id=instrument_id, limit=safe_limit, offset=safe_offset
            )
        }
        items = [
            {
                "decision": item.to_dict(),
                "cycle_status": cycles[item.cycle_id].status.value
                if item.cycle_id in cycles
                else "completed",
                "degraded": cycles[item.cycle_id].degraded
                if item.cycle_id in cycles
                else item.degraded,
                "conflicts": len(cycles[item.cycle_id].conflicts)
                if item.cycle_id in cycles
                else len(item.unresolved_conflicts),
                "advisory_only": True,
                "production_authority": False,
            }
            for item in decisions
        ]
        return {
            "items": items,
            "count": len(items),
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": len(items) == safe_limit,
            "kept_separate_from_financial_accounting": True,
        }

    def performance(self, agent_id: str = "") -> dict[str, Any]:
        items: list[Any] = list(self._store.performance(agent_id))
        if agent_id and not items:
            if agent_id not in DEFINITION_BY_ID:
                raise MultiAgentNotFoundError(f"unknown multi-agent specialist {agent_id!r}")
            config = self._config(agent_id)
            items = [
                {
                    "agent_id": agent_id,
                    "executions": 0,
                    "average_latency_ms": None,
                    "error_rate": 0.0,
                    "validation_failure_rate": 0.0,
                    "missing_evidence_rate": 0.0,
                    "historical_quality": None,
                    "health": (
                        AgentHealth.DISABLED.value
                        if not config.enabled
                        else AgentHealth.HEALTHY.value
                    ),
                    "provider": config.provider,
                    "model_version": config.model_version,
                    "last_execution": "",
                    "reliability_basis": "no executions yet; no quality assumed",
                }
            ]
        return {
            "phase": MULTI_AGENT_PHASE,
            "items": [item.to_dict() if hasattr(item, "to_dict") else item for item in items],
            "count": len(items),
            "historical_quality": (
                "not computed: no validated per-agent predictive scoring is stored"
            ),
            "reliability_note": (
                "successful response rate is not predictive quality; historical_quality "
                "stays null until a valid out-of-sample evaluation exists"
            ),
        }

    def latest_handoff(
        self, *, instrument_id: str = "", exchange: str = "", horizon: str = ""
    ) -> dict[str, Any] | None:
        """The newest handoff for an instrument/exchange/horizon, or ``None``.

        This is a read-only lookup for evidence consumers such as the AT-03
        decision path.  It never runs a cycle, never mutates a decision and never
        grants authority; a cycle without a decision is skipped.
        """

        instrument = str(instrument_id or "").strip()
        if not instrument:
            return None
        wanted_exchange = str(exchange or "").strip().upper()
        wanted_horizon = canonical_horizon(horizon) if str(horizon or "").strip() else ""
        completed = {
            item.cycle_id for item in self._store.cycles(status="completed", limit=50)
        }
        for decision in self._store.decisions(instrument_id=instrument, limit=20):
            if wanted_exchange and decision.exchange.upper() != wanted_exchange:
                continue
            if wanted_horizon and canonical_horizon(decision.horizon) != wanted_horizon:
                continue
            if decision.cycle_id not in completed:
                continue
            cycle = self._store.cycle(decision.cycle_id)
            if cycle is None:
                continue
            body = dict(self.handoff(decision.cycle_id))
            body["decided_at"] = decision.decided_at.isoformat()
            body["cycle_status"] = cycle.status
            return body
        return None

    def handoff(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._store.cycle(cycle_id)
        if cycle is None:
            raise MultiAgentNotFoundError(f"decision cycle {cycle_id!r} was not found")
        decision = self._store.decision(cycle_id)
        if decision is None:
            raise MultiAgentNotFoundError(
                f"decision cycle {cycle_id!r} has no advisory decision to hand off"
            )
        signal = decision.action in {RecommendationAction.BUY, RecommendationAction.SELL}
        return {
            "phase": MULTI_AGENT_PHASE,
            "cycle_id": cycle_id,
            "decision_id": decision.decision_id,
            "instrument_id": decision.instrument_id,
            "exchange": decision.exchange,
            "horizon": decision.horizon,
            "recommendation": decision.action.value,
            "advisory_only": True,
            "production_authority": False,
            "order_submitted": False,
            "eligible_research_signal": signal,
            "risk_review_required": True,
            "requires_nm08_reassessment": True,
            "requires_pi12_orchestration": True,
            "requires_operator_permission": True,
            "execution_path": [
                "PI-12 autonomy orchestration",
                "NM-08 authoritative risk assessment",
                "NM-10 authoritative paper execution",
            ],
            "authority": {
                "risk": "NM-08",
                "paper_execution": "NM-10",
                "autonomy": "PI-12",
                "agents_can_submit_orders": False,
                "agents_can_change_modes": False,
                "agents_can_override_kill_switch": False,
                "agents_can_activate_policies": False,
            },
            "decision": decision.to_dict(),
            "restrictions": {
                "no_direct_execution": True,
                "no_broker": True,
                "no_live_trading": True,
                "no_portfolio_mutation": True,
                "no_automatic_policy_promotion": True,
            },
            "note": (
                "This contract is research evidence.  A production paper action must be "
                "re-derived by PI-12, approved by NM-08 and submitted only by NM-10."
            ),
        }
