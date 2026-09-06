"""API metadata models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
                ApiRoute("GET", "/system/harden", "Release hardening report."),
                ApiRoute("GET", "/system/package", "Runtime package manifest."),
                ApiRoute("POST", "/ask", "Route a natural-language request through NovaControl.", authenticated=True),
                ApiRoute("POST", "/brain/mode", "Switch the chat brain between auto, llm, scratch, and cloud.", authenticated=True),
                ApiRoute("GET", "/brain/mode", "Inspect the active brain mode and provider.", authenticated=True),
                ApiRoute("GET", "/brain/cloud/presets", "List cloud LLM provider presets (no secrets).", authenticated=True),
                ApiRoute("POST", "/brain/cloud", "Install a cloud LLM (ChatGPT/Gemini/Groq) with a local API key.", authenticated=True),
                ApiRoute("POST", "/brain/cloud/clear", "Remove the cloud LLM config and its API key.", authenticated=True),
                ApiRoute("POST", "/chat/clear", "Clear the server-side chat conversation memory.", authenticated=True),
                ApiRoute("GET", "/tasks", "List tracked task records.", authenticated=True),
                ApiRoute("POST", "/tasks/delete", "Delete one tracked task record by id.", authenticated=True),
                ApiRoute("POST", "/tasks/clear", "Delete all tracked task records.", authenticated=True),
                ApiRoute("POST", "/improve", "Create a self-improvement plan.", authenticated=True),
                ApiRoute("POST", "/improve/workflow", "Create a friendly self-improvement workflow.", authenticated=True),
                ApiRoute("POST", "/improve/preview", "Run a temporary self-improvement preview.", authenticated=True),
                ApiRoute("POST", "/improve/approve", "Approve a preview for code changes.", authenticated=True),
                ApiRoute("POST", "/learn", "Run a local feedback learning cycle.", authenticated=True),
                ApiRoute("POST", "/train", "Run bounded autonomous local learning iterations.", authenticated=True),
                ApiRoute("POST", "/plan", "Create and optionally execute a plan.", authenticated=True),
                ApiRoute("POST", "/command/plan", "Plan a natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/command/execute", "Execute an approved natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/desktop/plan", "Plan an approval-gated desktop action.", authenticated=True),
                ApiRoute("POST", "/desktop/execute", "Execute an approved desktop action.", authenticated=True),
                ApiRoute("GET", "/phone/status", "Inspect phone bridge status.", authenticated=True),
                ApiRoute("POST", "/phone/connect", "Run the phone bridge pairing flow.", authenticated=True),
                ApiRoute("GET", "/vision/status", "Vision capability report: model availability and open bug count.", authenticated=True),
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
