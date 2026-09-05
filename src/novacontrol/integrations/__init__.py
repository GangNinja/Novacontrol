"""External integrations subsystem."""

from novacontrol.integrations.llm import (
    EchoLLMProvider,
    LLMProviderRegistry,
    OpenAICompatibleLLMProvider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    detect_ollama,
)
from novacontrol.integrations.redis import (
    RedisEventBus,
    RedisTtlCache,
    build_redis_cache,
    build_redis_event_bus,
)
from novacontrol.integrations.registry import IntegrationDefinition, IntegrationRegistry
from novacontrol.integrations.resilient_llm import ResilientLLMProvider

__all__ = [
    "EchoLLMProvider",
    "IntegrationDefinition",
    "IntegrationRegistry",
    "LLMProviderRegistry",
    "OpenAICompatibleLLMProvider",
    "RedisEventBus",
    "RedisTtlCache",
    "ResilientLLMProvider",
    "build_llm_provider_from_environment",
    "build_ollama_provider",
    "build_redis_cache",
    "build_redis_event_bus",
    "detect_ollama",
]
