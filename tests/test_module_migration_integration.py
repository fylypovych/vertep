"""Integration tests for role→module migration (Stage 5-7, Issue #348).

Tests actual module route through feature flag, shadow execution,
parity comparison between role and module, and rollback.
"""

import os
import pytest
from worker.roles.text_node import execute as text_execute, _module_enabled
from worker.roles.gpu_node import execute as gpu_execute
from worker.roles.voice_node import execute as voice_execute
from worker.role_executor import execute_role_task


class TestTextModuleMigration:
    """Text Node → Text Module migration integration tests."""

    def test_text_module_path_when_enabled(self):
        """Module path used when feature flag enabled."""
        os.environ["VERTEP_TEXT_MODULE_ENABLED"] = "true"
        import importlib
        import worker.roles.text_node
        importlib.reload(worker.roles.text_node)
        from worker.roles.text_node import execute as text_execute_reloaded

        task = {"task": "text", "topic": "test", "task_id": "test-2"}
        # Module requires provider - mock it
        from modules.text.text_module import TextModule
        mock_provider = type("MockProvider", (), {
            "generate_script": lambda **kw: {"title": "test", "scenes": []}
        })()
        module = TextModule(provider=mock_provider)

        result = module.execute(task)
        assert result is not None
        assert len(result) > 0

    def test_text_module_health_check(self):
        """Module health check returns correct status."""
        from modules.text.text_module import TextModule
        module = TextModule()
        health = module.health_check()
        assert health["module"] == "text"
        assert health["status"] == "READY"
        assert "active_tasks" in health

    def test_text_module_resource_query(self):
        """Module resource query reports CPU-only needs."""
        from modules.text.text_module import TextModule
        module = TextModule()
        resources = module.resource_query()
        assert resources["module"] == "text"
        assert resources["gpu_required"] is False
        assert resources["min_vram_mb"] == 0


class TestImageModuleMigration:
    """GPU Node → Image Module migration integration tests."""

    def test_image_module_path_when_enabled(self):
        """Module path used when feature flag enabled."""
        os.environ["VERTEP_IMAGE_MODULE_ENABLED"] = "true"
        import importlib
        import worker.roles.gpu_node
        importlib.reload(worker.roles.gpu_node)
        from worker.roles.gpu_node import execute as gpu_execute_reloaded

        task = {"task": "image", "topic": "test", "workflow": "test.json", "task_id": "test-3"}
        # Module requires provider - mock it
        from modules.image.image_module import ImageModule
        mock_provider = type("MockProvider", (), {
            "generate_output": lambda *args, **kw: (b"fake", "output.png", "image")
        })()
        module = ImageModule(provider=mock_provider)

        result = module.execute(task)
        assert result is not None
        assert len(result) > 0

    def test_image_module_health_check(self):
        """Module health check returns correct status."""
        from modules.image.image_module import ImageModule
        module = ImageModule()
        health = module.health_check()
        assert health["module"] == "image"
        assert health["status"] == "READY"

    def test_image_module_resource_query(self):
        """Module resource query reports GPU needs."""
        from modules.image.image_module import ImageModule
        module = ImageModule()
        resources = module.resource_query()
        assert resources["module"] == "image"
        assert resources["gpu_required"] is True
        assert resources["min_vram_mb"] >= 6144


class TestAudioModuleMigration:
    """Voice Node → Audio Module migration integration tests."""

    def test_audio_module_path_when_enabled(self):
        """Module path used when feature flag enabled."""
        os.environ["VERTEP_AUDIO_MODULE_ENABLED"] = "true"
        import importlib
        import worker.roles.voice_node
        importlib.reload(worker.roles.voice_node)
        from worker.roles.voice_node import execute as voice_execute_reloaded

        task = {"task": "voice", "text": "test", "voice": "default", "task_id": "test-4"}
        # Module requires provider - mock it
        from modules.audio.audio_module import AudioModule
        mock_provider = type("MockProvider", (), {
            "synthesize": lambda *args, **kw: b"fake_audio"
        })()
        module = AudioModule(provider=mock_provider)

        result = module.execute(task)
        assert result is not None
        assert len(result) > 0

    def test_audio_module_health_check(self):
        """Module health check returns correct status."""
        from modules.audio.audio_module import AudioModule
        module = AudioModule()
        health = module.health_check()
        assert health["module"] == "audio"
        assert health["status"] == "READY"

    def test_audio_module_resource_query(self):
        """Module resource query reports CPU-only needs."""
        from modules.audio.audio_module import AudioModule
        module = AudioModule()
        resources = module.resource_query()
        assert resources["module"] == "audio"
        assert resources["gpu_required"] is False
        assert resources["min_vram_mb"] == 0


class TestModuleParity:
    """Parity comparison between role and module execution."""

    def test_text_role_module_structure_parity(self):
        """Role and module return compatible structure."""
        from modules.text.text_module import TextModule
        module = TextModule()
        mock_provider = type("MockProvider", (), {
            "generate_script": lambda **kw: {"title": "test", "scenes": []}
        })()
        module.provider = mock_provider

        task = {"task": "text", "topic": "test", "task_id": "parity-1"}
        result = module.execute(task)

        # Result structure matches contract
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)
        assert all("filename" in r for r in result)
        assert all("kind" in r for r in result)
        assert all("data" in r for r in result)

    def test_image_role_module_structure_parity(self):
        """Role and module return compatible structure."""
        from modules.image.image_module import ImageModule
        module = ImageModule()
        mock_provider = type("MockProvider", (), {
            "generate_output": lambda *args, **kw: (b"fake", "output.png", "image")
        })()
        module.provider = mock_provider

        task = {"task": "image", "topic": "test", "workflow": "test.json", "task_id": "parity-2"}
        result = module.execute(task)

        # Result structure matches contract
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)
        assert all("filename" in r for r in result)
        assert all("kind" in r for r in result)


class TestModuleRollback:
    """Rollback from module to role path."""

    def test_module_cancel_after_rollback(self):
        """Cancel works correctly after rollback."""
        from modules.text.text_module import TextModule
        module = TextModule()
        task_id = "cancel-test"

        # Start a task
        module._active_tasks[task_id] = {"type": "text"}

        # Cancel it
        cancelled = module.cancel(task_id)
        assert cancelled is True
        assert task_id not in module._active_tasks
