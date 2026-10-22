"""Integration module for Architecture 2.0 components.

Wires together the Workflow Engine, Module System, Dispatcher, and GPU Provider
abstraction into a cohesive system that can be initialized and used by CORE.
"""

from __future__ import annotations

from core.workflow.engine import WorkflowEngine
from core.module.registry import ModuleRegistry
from core.dispatcher.dispatcher import Dispatcher
from core.gpu.provider_registry import GPUProviderRegistry
from core.gpu.local_mode import LocalModelProvider


def initialize_architecture_20(core_dir: str | None = None) -> dict[str, Any]:
    """Initialize all Architecture 2.0 components.

    Sets up the complete stack:
    - Workflow Engine for job management
    - Module System for discoverable modules
    - Dispatcher for task assignment
    - GPU Provider Abstraction with local fallback

    Returns:
        Dictionary of initialized components keyed by name
    """
    working_dir = Path(core_dir) if core_dir else Path(__file__).resolve().parent.parent

    # Initialize Workflow Engine
    state = __import__("core.workflow.state", fromlist=["WorkflowState"]).WorkflowState()
    engine = WorkflowEngine(state)

    # Initialize Module Registry
    module_registry = ModuleRegistry()

    # Initialize Dispatcher
    dispatcher = Dispatcher()

    # Initialize GPU Provider Registry with local fallback
    gpu_registry = GPUProviderRegistry()
    local_provider = LocalModelProvider(configured=False)
    gpu_registry.register_provider("local_model", local_provider, priority=10)

    # Return all components for use by CORE
    return {
        "workflow_engine": engine,
        "module_registry": module_registry,
        "dispatcher": dispatcher,
        "gpu_provider_registry": gpu_registry,
        "local_model_provider": local_provider,
        "initialization_dir": str(working_dir),
    }