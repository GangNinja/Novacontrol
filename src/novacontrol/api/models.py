"""API metadata models and request bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


# ── Request bodies ────────────────────────────────────────────────
#
# Pydantic models give the API contract real teeth: a missing required key is
# a 422 with a field-level error (not a KeyError masked as a 500), and empty
# or whitespace-only input is rejected at the edge with a 400 before it can
# reach — and crash — the research pipeline.


class AskRequest(BaseModel):
    """POST /ask — one natural-language request."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"request": "open chrome"}]}
    )

    request: str

    @field_validator("request")
    @classmethod
    def _request_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request must not be empty or whitespace")
        return value


class BrainDecideRequest(BaseModel):
    """POST /brain/decide — trace an utterance through the routing gates."""

    text: str

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be empty or whitespace")
        return value


class ExploreRequest_(BaseModel):
    """POST /explore — research one topic.

    Named with a trailing underscore to avoid shadowing the application-layer
    ExploreRequest dataclass imported by this module's consumers.
    """

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"topic": "how do black holes form"}]}
    )

    topic: str
    depth: str = "deep"
    include_videos: bool = True
    max_sources: int = 6
    max_videos: int = 5
    last_topic: str = ""
    prior_topics: list[str] = []
    # Run-scoped correlation id minted by the client: echoed on every
    # explore.progress event so concurrent researches interleave in the UI
    # without mixing rows. Optional; the server mints one when absent.
    correlation_id: str = ""

    @field_validator("topic")
    @classmethod
    def _topic_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic must not be empty or whitespace")
        return value


class CommandPlanRequest(BaseModel):
    """POST /command/plan — plan one natural device command."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"command": "open calculator"}]}
    )

    command: str

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty or whitespace")
        return value


@dataclass(frozen=True, slots=True)
class ApiRoute:
    method: str
    path: str
    description: str
    authenticated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "description": self.description,
            "authenticated": self.authenticated,
        }


@dataclass(frozen=True, slots=True)
class ApiSurface:
    routes: tuple[ApiRoute, ...]
    websocket_paths: tuple[str, ...]

    @classmethod
    def default(cls) -> "ApiSurface":
        return cls(
            routes=(
                ApiRoute("GET", "/", "Local web platform."),
                ApiRoute("GET", "/health", "Health check."),
                ApiRoute("GET", "/status", "Runtime status."),
                ApiRoute("GET", "/system/health", "Aggregated system health."),
                ApiRoute("GET", "/system/telemetry", "Live machine telemetry for the Command Center (CPU, memory, storage, GPU, network, battery, temperature)."),
                ApiRoute("GET", "/system/harden", "Release hardening report."),
                ApiRoute("GET", "/system/package", "Runtime package manifest."),
                ApiRoute("POST", "/ask", "Route a natural-language request through NovaControl.", authenticated=True),
                ApiRoute("POST", "/brain/mode", "Switch the chat brain between auto, llm, scratch, and cloud.", authenticated=True),
                ApiRoute("GET", "/brain/mode", "Inspect the active brain mode and provider.", authenticated=True),
                ApiRoute("GET", "/brain/cloud/presets", "List cloud LLM provider presets (no secrets).", authenticated=True),
                ApiRoute("POST", "/brain/cloud", "Install a cloud LLM (ChatGPT/Gemini/Groq) with a local API key.", authenticated=True),
                ApiRoute("POST", "/brain/cloud/test", "Ping a cloud provider with the pasted key (one tiny completion; nothing is saved).", authenticated=True),
                ApiRoute("POST", "/brain/cloud/clear", "Remove the cloud LLM config and its API key.", authenticated=True),
                ApiRoute("GET", "/brain/ollama/models", "List local Ollama models for the brain model picker (fresh probe).", authenticated=True),
                ApiRoute("POST", "/brain/local/model", "Pin the local brain to a specific Ollama model (empty clears the pick).", authenticated=True),
                ApiRoute("POST", "/chat/clear", "Clear the server-side chat conversation memory and the shared transcript.", authenticated=True),
                ApiRoute("GET", "/chat/history", "Read the shared server-persisted chat thread (same for every browser).", authenticated=True),
                ApiRoute("POST", "/chat/history", "Append a chat turn to the shared thread (action=migrate imports a browser's old history once).", authenticated=True),
                ApiRoute("GET", "/tasks", "List tracked task records.", authenticated=True),
                ApiRoute("POST", "/tasks/delete", "Delete one tracked task record by id.", authenticated=True),
                ApiRoute("POST", "/tasks/clear", "Delete all tracked task records (undoable for a short window).", authenticated=True),
                ApiRoute("POST", "/tasks/clear/undo", "Restore the tasks wiped by a recent /tasks/clear (one-shot token).", authenticated=True),
                ApiRoute("POST", "/improve", "Create a self-improvement plan.", authenticated=True),
                ApiRoute("POST", "/improve/workflow", "Create a friendly self-improvement workflow.", authenticated=True),
                ApiRoute("POST", "/improve/preview", "Run a temporary self-improvement preview.", authenticated=True),
                ApiRoute("POST", "/improve/approve", "Approve a preview for code changes.", authenticated=True),
                ApiRoute("POST", "/learn", "Run a local feedback learning cycle.", authenticated=True),
                ApiRoute("POST", "/knowledge/teach", "Teach: persist a typed fact as durable recallable knowledge.", authenticated=True),
                ApiRoute("GET", "/knowledge", "List taught knowledge, optionally filtered by a query.", authenticated=True),
                ApiRoute("POST", "/knowledge/recall", "Recall taught knowledge matching a query.", authenticated=True),
                ApiRoute("POST", "/train", "Run bounded autonomous local learning iterations.", authenticated=True),
                ApiRoute("POST", "/brain/decide", "Trace an utterance through the routing gates with a rung preview.", authenticated=True),
                ApiRoute("POST", "/plan", "Create and optionally execute a plan.", authenticated=True),
                ApiRoute("POST", "/plan/code", "Plan a coding task: language-aware steps plus a drafted code artifact.", authenticated=True),
                ApiRoute("POST", "/build/save", "Save a drafted Build artifact to the build_workspace folder on disk.", authenticated=True),
                ApiRoute("POST", "/command/plan", "Plan a natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/command/execute", "Execute an approved natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/desktop/plan", "Plan an approval-gated desktop action.", authenticated=True),
                ApiRoute("POST", "/desktop/execute", "Execute an approved desktop action.", authenticated=True),
                ApiRoute("POST", "/browser/plan", "Plan an approval-gated browser action (navigate, search + extract).", authenticated=True),
                ApiRoute("POST", "/browser/execute", "Execute an approved browser action (search returns the top results).", authenticated=True),
                ApiRoute("GET", "/phone/status", "Inspect phone bridge status.", authenticated=True),
                ApiRoute("POST", "/phone/connect", "Run the phone bridge pairing flow.", authenticated=True),
                ApiRoute("GET", "/vision/status", "Vision capability report: model availability and open bug count.", authenticated=True),
                ApiRoute("POST", "/vision/model", "Install a multimodal vision model (ollama or a cloud provider) for element location.", authenticated=True),
                ApiRoute("POST", "/vision/model/clear", "Remove the configured vision model; element location returns to OCR-only.", authenticated=True),
                ApiRoute("POST", "/vision/describe", "Capture the screen and describe it (vision model or window probe).", authenticated=True),
                ApiRoute("POST", "/vision/click", "Vision-locate a labeled element on screen, click it, and verify.", authenticated=True),
                ApiRoute("GET", "/intelligence", "Global Intelligence Layer: telemetry, findings, and capabilities.", authenticated=True),
                ApiRoute("GET", "/bugs", "List recorded bugs (what failed, where, and when).", authenticated=True),
                ApiRoute("POST", "/bugs/{bug_id}/fix", "Mark a recorded bug as fixed.", authenticated=True),
                ApiRoute("POST", "/bugs/clear-fixed", "Remove all bugs already marked fixed.", authenticated=True),
                ApiRoute("POST", "/phone/plan", "Plan an approval-gated phone action.", authenticated=True),
                ApiRoute("POST", "/phone/execute", "Execute an approved phone action.", authenticated=True),
                ApiRoute("POST", "/agent/run", "Run the full agentic loop for a natural-language goal.", authenticated=True),
                ApiRoute("GET", "/agent/metrics", "Agentic evaluation metrics (success, recovery, verification rates).", authenticated=True),
                ApiRoute("GET", "/agent/knowledge", "Application knowledge graph: workflows, confidence, freshness.", authenticated=True),
                ApiRoute("POST", "/explore", "Research a topic and return an Explore report.", authenticated=True),
                ApiRoute("GET", "/explore/trending", "Current daily research topics from live top-story news (rotating window).", authenticated=True),
                ApiRoute("GET", "/activity", "Recent completed actions (commands, research, learning) for the web timeline.", authenticated=True),
                ApiRoute("GET", "/events/stream", "Live activity channel: the application EventBus as SSE."),
                ApiRoute("GET", "/settings", "Read local user settings.", authenticated=True),
                ApiRoute("POST", "/settings", "Update local user settings.", authenticated=True),
                ApiRoute("GET", "/plugins", "List plugin API records.", authenticated=True),
            ),
            websocket_paths=("/ws/events",),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [route.to_dict() for route in self.routes],
            "websocket_paths": self.websocket_paths,
        }
