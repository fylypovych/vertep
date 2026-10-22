GPU Provider Contract
=====================

1. Абстрактний інтерфейс провайдера (query capability, submit inference, cancel inference, get resource usage)
---------------------------------
```python
class GPUProvider(ABC):
    @abstractmethod
    def query_capability(self, request: dict) -> dict:
        """Query provider capability.
        
        Request: {type: "image_generation"|"video_generation", 
                   model: str, 
                   params: dict}
        Response: {available: bool, details: dict}
        """
    
    @abstractmethod
    def submit_inference(self, task: dict) -> dict:
        """Submit inference task.
        
        Task: {task_id, prompt, character_config, params}
        Response: {task_id, status: "queued"|"running"|"complete"|"failed", 
                   result_ref: str, vram_used_mb: int}
        """
    
    @abstractmethod
    def cancel_inference(self, task_id: str) -> dict:
        """Cancel running inference.
        
        Response: {task_id, cancelled: bool, cleanup: bool}
        """
    
    @abstractmethod
    def get_resource_usage(self) -> dict:
        """Get current VRAM/CPU usage.
        
        Response: {vram_total_mb: int, vram_used_mb: int, 
                   cpu_total_cores: int, cpu_used_cores: int, 
                   memory_used_mb: int, temperature_celsius: int|none}
        """
```

2. Контракт provider metadata (name, type, capabilities, priority)
---------------------------------
```yaml
provider:
  name: "ollama"
  type: "local_model"
  capabilities: ["image_generation", "text_generation", "voice_synthesis"]
  priority: 1  # 1 = highest, higher = lower priority
  supports_amd: false
  supports_nvidia: true
  min_vram_mb: 4096
  max_concurrent_tasks: 4
```

3. Контракт fallback маршрутизації (provider list, priority, readiness check)
---------------------------------
- Providers ordered by priority list in configuration.
- Fallback sequence: provider[0] → provider[1] → ... → provider[n].
- Readiness check: each provider must respond `ready: true` before task assignment.
- If primary provider fails or times out, automatically switch to next in list.
- Transparent reason logging: each fallback event logged with reason (timeout, error, capacity exceeded).

4. Контракт ресурсного lease (GPU allocation, VRAM tracking, release)
---------------------------------
- Lease granted for task duration + timeout margin.
- VRAM tracking: provider reports actual VRAM used during task.
- Lease release on: task completion, task cancellation, worker offline, lease expiry.
- Failure to release: CORE force-releases after timeout, logs warning.

5. Edge cases
-----------
- Provider completely unavailable: fallback to next provider in list.
- VRAM exhaustion mid-task: task failed, VRAM released, user notified.
- Provider responds but returns error: counted as failure, fallback triggered.
- Mixed provider types (NVIDIA/AMD): fallback only within same type unless configured otherwise.
- Local mode: if local-only mode enabled, only local providers accepted, external blocked.