"""External integrations subsystem."""

from novacontrol.integrations.llm import (
    CLOUD_LLM_PRESETS,
    AnthropicMessagesProvider,
    EchoLLMProvider,
    LLMProviderRegistry,
    OpenAICompatibleLLMProvider,
    build_cloud_provider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    build_vision_provider,
    cloud_llm_presets,
    detect_ollama,
    get_cloud_preset,
    is_ollama_vision_model,
    make_ollama_reprobe,
    ollama_models,
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
    "CLOUD_LLM_PRESETS",
    "AnthropicMessagesProvider",
    "EchoLLMProvider",
    "IntegrationDefinition",
    "IntegrationRegistry",
    "LLMProviderRegistry",
    "OpenAICompatibleLLMProvider",
    "make_ollama_reprobe",
    "RedisEventBus",
    "RedisTtlCache",
    "ResilientLLMProvider",
    "build_cloud_provider",
    "build_llm_provider_from_environment",
    "build_ollama_provider",
    "build_redis_cache",
    "build_redis_event_bus",
    "build_vision_provider",
    "cloud_llm_presets",
    "detect_ollama",
    "get_cloud_preset",
    "is_ollama_vision_model",
    "ollama_models",
]
