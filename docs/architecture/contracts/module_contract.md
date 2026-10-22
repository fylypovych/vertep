Module Contract
===============

1. Структура module manifest (name, version, dependencies, capabilities, resource requirements)
---------------------------------
```yaml
manifest:
  name: "video_enhancer"
  version: "1.0.0"
  description: "Enhances video quality using AI"
  dependencies:
    - name: "core_runtime"
      minimum_version: "0.0.2"
      maximum_version: "0.1.0"
  capabilities:
    - "video_upscale"
    - "color_correction"
    - "frame_interpolation"
  resource_requirements:
    min_vram_mb: 4096
    min_cpu_cores: 4
    supports_gpu: true
    supports_amd: false
```

2. Контракт module API (execute, cancel, health check, resource query)
---------------------------------
- `execute(task_definition, artifacts)` → result
- `cancel(task_id)` → acknowledgment
- `health_check()` → {status: "HEALTHY"|"UNHEALTHY", details: any}
- `resource_query(req_type, req_value)` → {available, total, used}

3. Контракт module discovery (registration, capability reporting, version negotiation)
---------------------------------
- Worker registers capabilities during onboarding.
- CORE maintains registry of available modules.
- Version negotiation: module declares compatible CORE version range.
- Capability update: worker can report updated capabilities via heartbeat.

4. Контракт module lifecycle (install, load, unload, uninstall)
---------------------------------
- `install(module_path)` → manifest validated, registered
- `load(module_name)` → module activated for task dispatch
- `unload(module_name)` → module deactivated, resources freed
- `uninstall(module_name)` → registry entry removed, artifacts cleaned

5. Edge cases
-----------
- Circular dependencies between modules: reject during install.
- Missing dependency: reject install, suggest alternative.
- Version conflict: reject if installed module version outside compatible range.
- Resource insufficient: reject install if min VRAM/CPU not met.
- Orphaned module: unload removes from active dispatch but keeps registry entry.