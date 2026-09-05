from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from novacontrol.automation import AutomationManager, AutomationStep, AutomationWorkflowStatus

from novacontrol.integrations import (
    EchoLLMProvider,
    IntegrationDefinition,
    IntegrationRegistry,
    LLMProviderRegistry,
    OpenAICompatibleLLMProvider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    detect_ollama,
)
from novacontrol.knowledge import KnowledgeBase
from novacontrol.projects import ProjectManager, TaskStatus
from novacontrol.scheduler import InMemoryScheduler, ScheduledTaskStatus
from novacontrol.skills import FunctionSkill, SkillInvocation, SkillRegistry, SkillSchema, SkillStatus


class RemainingModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_registry_invokes_function_skill(self) -> None:
        registry = SkillRegistry()
        registry.register(
            FunctionSkill(
                SkillSchema("greet", "Greet a user", ("name",)),
                lambda inputs: {"message": f"Hello {inputs['name']}"},
            )
        )

        result = await registry.invoke(SkillInvocation("greet", {"name": "Nova"}))

        self.assertEqual(result.status, SkillStatus.COMPLETED)
        self.assertEqual(result.output["message"], "Hello Nova")

    async def test_scheduler_runs_due_tasks(self) -> None:
        scheduler = InMemoryScheduler()
        task = scheduler.schedule_once("demo", delay_seconds=-1)
        seen: list[str] = []

        async def handler(scheduled_task):
            seen.append(scheduled_task.id)

        completed = await scheduler.run_due(handler)

        self.assertEqual(seen, [task.id])
        self.assertEqual(completed[0].status, ScheduledTaskStatus.COMPLETED)

    async def test_project_manager_tracks_progress(self) -> None:
        manager = ProjectManager()
        project = manager.create_project("NovaControl")
        project = manager.add_task(project.id, "Build core")
        task_id = project.tasks[0].id
        project = manager.set_task_status(project.id, task_id, TaskStatus.DONE)

        self.assertEqual(project.progress(), 1.0)

    async def test_knowledge_base_searches_articles(self) -> None:
        knowledge = KnowledgeBase()
        knowledge.add_article("Event architecture", "Modules communicate through events")

        results = knowledge.search("events")

        self.assertEqual(results[0].article.title, "Event architecture")

    async def test_automation_manager_tracks_workflow_status(self) -> None:
        manager = AutomationManager()
        workflow = manager.create_workflow(
            "Morning setup",
            (AutomationStep("Open editor", "desktop.open", {"app": "Code"}),),
        )
        updated = manager.mark_status(workflow.id, AutomationWorkflowStatus.READY)

        self.assertEqual(updated.status, AutomationWorkflowStatus.READY)

    async def test_integration_and_llm_registries(self) -> None:
        integrations = IntegrationRegistry()
        integrations.register(IntegrationDefinition("github", "GitHub"))
        llms = LLMProviderRegistry()
        provider = EchoLLMProvider()
        llms.register(provider)

        completion = await provider.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(integrations.get("github").provider, "GitHub")
        self.assertEqual(completion, "hello")

    async def test_openai_compatible_provider_uses_transport(self) -> None:
        def transport(url, headers, payload):
            return {
                "choices": [
                    {
                        "message": {
                            "content": f"{payload['model']}:{payload['messages'][0]['content']}"
                        }
                    }
                ]
            }

        provider = OpenAICompatibleLLMProvider(
            name="test-openai-compatible",
            base_url="https://api.example.com",
            api_key="secret",
            model="demo-model",
            transport=transport,
        )

        completion = await provider.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(completion, "demo-model:hello")

    async def test_environment_provider_requires_explicit_external_llm_opt_in(self) -> None:
        env = {
            "NOVACONTROL_LLM_MODEL": "demo-model",
            "NOVACONTROL_LLM_BASE_URL": "https://api.example.com",
            "NOVACONTROL_LLM_API_KEY": "secret",
            "NOVACONTROL_DISABLE_OLLAMA": "1",
        }
        provider = build_llm_provider_from_environment(env)
        enabled = build_llm_provider_from_environment({
            **env,
            "NOVACONTROL_ENABLE_EXTERNAL_LLM": "true",
        })

        self.assertIsInstance(provider, EchoLLMProvider)
        self.assertIsInstance(enabled, OpenAICompatibleLLMProvider)

    # ── Ollama auto-detection tests ───────────────────────

    def test_detect_ollama_returns_none_when_not_running(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            result = detect_ollama("http://127.0.0.1:19999")
            self.assertIsNone(result)
        finally:
            llm_mod._ollama_cache = old_cache

    def test_detect_ollama_returns_models_when_running(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        fake_response = {"models": [{"name": "llama3.2:3b"}, {"name": "phi3:mini"}]}
        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            with patch("novacontrol.integrations.llm.urlopen") as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda *a: None
                mock_open.return_value.read.return_value = json.dumps(fake_response).encode()
                result = detect_ollama("http://127.0.0.1:11434")
            self.assertIsNotNone(result)
            self.assertEqual(result["models"], ["llama3.2:3b", "phi3:mini"])
        finally:
            llm_mod._ollama_cache = old_cache

    def test_detect_ollama_returns_none_when_no_models(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            with patch("novacontrol.integrations.llm.urlopen") as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda *a: None
                mock_open.return_value.read.return_value = json.dumps({"models": []}).encode()
                result = detect_ollama("http://127.0.0.1:11434")
            self.assertIsNone(result)
        finally:
            llm_mod._ollama_cache = old_cache

    def test_build_ollama_provider_returns_none_when_not_running(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            result = build_ollama_provider("http://127.0.0.1:19999")
            self.assertIsNone(result)
        finally:
            llm_mod._ollama_cache = old_cache

    def test_build_ollama_provider_returns_provider_when_running(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        fake_response = {"models": [{"name": "phi3:mini"}, {"name": "llama3.2:3b"}]}
        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            with patch("novacontrol.integrations.llm.urlopen") as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda *a: None
                mock_open.return_value.read.return_value = json.dumps(fake_response).encode()
                provider = build_ollama_provider("http://127.0.0.1:11434")
            self.assertIsNotNone(provider)
            self.assertEqual(provider.name, "ollama")
            self.assertEqual(provider.model, "phi3:mini")
        finally:
            llm_mod._ollama_cache = old_cache

    def test_build_llm_provider_auto_detects_ollama(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        fake_response = {"models": [{"name": "mistral:7b"}]}
        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            with patch("novacontrol.integrations.llm.urlopen") as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda *a: None
                mock_open.return_value.read.return_value = json.dumps(fake_response).encode()
                provider = build_llm_provider_from_environment({})
            self.assertIsInstance(provider, OpenAICompatibleLLMProvider)
            self.assertEqual(provider.name, "ollama")
        finally:
            llm_mod._ollama_cache = old_cache

    def test_disable_ollama_env_var_skips_detection(self) -> None:
        import novacontrol.integrations.llm as llm_mod

        old_cache = llm_mod._ollama_cache
        llm_mod._ollama_cache = None
        try:
            with patch("novacontrol.integrations.llm.urlopen") as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda *a: None
                mock_open.return_value.read.return_value = json.dumps({"models": [{"name": "llama3"}]}).encode()
                provider = build_llm_provider_from_environment({"NOVACONTROL_DISABLE_OLLAMA": "1"})
            self.assertIsInstance(provider, EchoLLMProvider)
            mock_open.assert_not_called()
        finally:
            llm_mod._ollama_cache = old_cache


if __name__ == "__main__":
    unittest.main()
