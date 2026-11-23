"""Module Manifest - declarative definition of a Vertep module.

One user-facing module model covers both internal contracts: ``execution``
(work performed through Worker) and ``application`` (own API/UI/data in an
isolated container). The manifest describes the module *definition*; where it
is installed is a separate node/module instance (see ``instances.py``).

Schema negotiation: a manifest without ``schema_version`` is the legacy
1.x contract (name/version/capabilities/dependencies/resources) and is read
unchanged. ``2.x`` adds identity, compatibility, placement, API/UI, events,
permissions, secret references, migrations, services and health. Minor
versions are forward compatible; an unsupported major is rejected.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

SUPPORTED_SCHEMA_MAJORS = (1, 2)
CURRENT_SCHEMA_VERSION = "2.0"
LEGACY_SCHEMA_VERSION = "1.0"
MODULE_KINDS = ("execution", "application")
PLACEMENT_TARGETS = ("any", "isolated_lab")
SCALING_MODES = ("replicated", "single_owner")
INTERFACE_TYPES = ("api", "event", "extension_point", "capability")  # capability: shared Worker capability via CORE

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
_INTERFACE_VERSION = re.compile(r"\d+\.\d+")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PERMISSION = re.compile(r"[a-z][a-z0-9_.:-]*")
_SECRET_REF = re.compile(r"secret://[a-z0-9][a-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_ROUTE = re.compile(r"/[A-Za-z0-9_./{}-]*")
_DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9.-]*(?:/[A-Za-z0-9_.-]+)+")


def negotiate_schema_version(value: Any) -> str:
    """Return the accepted schema version or raise for an unsupported major."""
    if value is None:
        return LEGACY_SCHEMA_VERSION
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        raise ValueError(f"Invalid module schema_version: {value!r}")
    if int(value.split(".")[0]) not in SUPPORTED_SCHEMA_MAJORS:
        raise ValueError(f"Unsupported module schema_version major: {value}")
    return value


def _schema_major(version: str) -> int:
    return int(version.split(".")[0])


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid module {field}")
    return value


def _interface_version(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _INTERFACE_VERSION.fullmatch(value):
        raise ValueError(f"Invalid module {field} version; expected MAJOR.MINOR")
    return value


def _list_of_dicts(value: Any, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Module {field} must be a list of objects")
    return value


class ModuleManifest:
    """A module manifest describing identity, contracts and requirements."""

    def __init__(
        self,
        name: str,
        version: str,
        capabilities: List[str],
        dependencies: List[str],
        resource_requirements: Dict[str, Any],
        description: str = "",
        *,
        schema_version: str | None = None,
        module_id: str | None = None,
        kind: str = "execution",
        core_compatibility: Dict[str, str] | None = None,
        placement: Dict[str, Any] | None = None,
        provides: List[dict] | None = None,
        requires: List[dict] | None = None,
        api: Dict[str, Any] | None = None,
        ui: Dict[str, Any] | None = None,
        permissions: List[str] | None = None,
        secret_refs: List[dict] | None = None,
        migrations: List[dict] | None = None,
        services: List[dict] | None = None,
        health: Dict[str, Any] | None = None,
        publisher: str | None = None,
        provenance: Dict[str, Any] | None = None,
        container: Dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Module name must be a nonempty string")
        if not isinstance(capabilities, list) or any(not isinstance(cap, str) or not cap for cap in capabilities):
            raise ValueError("Capabilities must be nonempty strings")
        if not isinstance(dependencies, list):
            raise ValueError("Dependencies must be a list")
        for dep in dependencies:
            if isinstance(dep, str) and dep:
                continue
            if not isinstance(dep, dict) or not isinstance(dep.get("name"), str) or not dep["name"]:
                raise ValueError("Invalid module dependency")
            for field in ("min_version", "max_version"):
                if field in dep:
                    self._validate_version(dep[field])
        if not isinstance(resource_requirements, dict):
            raise ValueError("Resource requirements must be an object")
        for key in ("min_vram_mb", "min_cpu_cores", "min_ram_mb"):
            value = resource_requirements.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Invalid module resource requirement: {key}")
        self.name = name
        self.version = self._validate_version(version)
        self.capabilities = capabilities or []
        self.dependencies = dependencies or []
        self.resource_requirements = resource_requirements or {}
        self.description = description
        self.schema_version = negotiate_schema_version(schema_version)
        self._validate_contract(module_id, kind, core_compatibility, placement, provides, requires, api, ui,
                                permissions, secret_refs, migrations, services, health)
        self._validate_origin(publisher, provenance, container)

    # -- contract validation -------------------------------------------------
    def _validate_origin(self, publisher, provenance, container) -> None:
        """Publisher/provenance (trust) and the isolated application container."""
        if any(value is not None for value in (publisher, provenance, container)) and _schema_major(self.schema_version) < 2:
            raise ValueError("Extended module contract fields require schema_version 2.x")
        self.publisher = _identifier(publisher, "publisher") if publisher is not None else None
        self.provenance = None
        if provenance is not None:
            if not isinstance(provenance, dict) or set(provenance) - {"repository", "revision", "forked_from"}:
                raise ValueError("Module provenance accepts repository/revision/forked_from")
            for field in ("repository", "forked_from"):
                value = provenance.get(field)
                if (field == "repository" or value is not None) and (not isinstance(value, str) or not _REPOSITORY.fullmatch(value)):
                    raise ValueError(f"Invalid module provenance {field}")
            if "revision" in provenance and (not isinstance(provenance["revision"], str) or not re.fullmatch(r"[0-9a-f]{7,64}", provenance["revision"])):
                raise ValueError("Invalid module provenance revision")
            self.provenance = dict(provenance)
        self.container = None
        if container is not None:
            if not isinstance(container, dict) or set(container) - {"image", "port"}:
                raise ValueError("Module container accepts image/port")
            image, port = container.get("image"), container.get("port", 8080)
            if not isinstance(image, str) or not _DIGEST_IMAGE.fullmatch(image):
                raise ValueError("Module container image must be pinned by sha256 digest")
            if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
                raise ValueError("Module container port must be 1024-65535")
            self.container = {"image": image, "port": port}
        if self.kind == "application" and self.container is None:
            raise ValueError("Application modules run in their own container; container.image is required")
        if self.kind == "execution" and self.container is not None:
            raise ValueError("Execution modules run through Worker; container is for application modules")
        for item in self.secret_refs:
            # A module may only reference its own secret scope.
            if item["ref"].split("/")[2] != self.module_id.lower():
                raise ValueError("Module secret ref must use its own module scope")

    def _validate_contract(self, module_id, kind, core_compatibility, placement, provides, requires, api, ui,
                           permissions, secret_refs, migrations, services, health) -> None:
        extended = any(value not in (None, "execution", [], {}) for value in (
            module_id, kind, core_compatibility, placement, provides, requires, api, ui,
            permissions, secret_refs, migrations, services, health))
        if extended and _schema_major(self.schema_version) < 2:
            raise ValueError("Extended module contract fields require schema_version 2.x")
        self.module_id = _identifier(module_id if module_id is not None else self.name, "module_id")
        if kind not in MODULE_KINDS:
            raise ValueError(f"Invalid module kind: {kind!r}")
        self.kind = kind

        compatibility = core_compatibility or {}
        if not isinstance(compatibility, dict) or set(compatibility) - {"min", "max"}:
            raise ValueError("core_compatibility accepts only min/max")
        for bound in compatibility.values():
            self._validate_version(bound)
        self.core_compatibility = dict(compatibility)

        placement = dict(placement or {})
        if set(placement) - {"target", "scaling", "requires_gpu"}:
            raise ValueError("Unknown module placement field")
        target = placement.get("target", "any")
        if target not in PLACEMENT_TARGETS:
            raise ValueError(f"Invalid module placement target: {target!r}")
        default_scaling = "single_owner" if kind == "application" else "replicated"
        scaling = placement.get("scaling", default_scaling)
        if scaling not in SCALING_MODES:
            raise ValueError(f"Invalid module scaling mode: {scaling!r}")
        if kind == "application" and scaling != "single_owner":
            # Replication of stateful application data has no defined contract yet.
            raise ValueError("Application modules are single_owner; replication is not defined")
        requires_gpu = placement.get("requires_gpu", self.resource_requirements.get("min_vram_mb", 0) > 0)
        if not isinstance(requires_gpu, bool):
            raise ValueError("placement.requires_gpu must be boolean")
        self.placement = {"target": target, "scaling": scaling, "requires_gpu": requires_gpu}

        self.provides = []
        seen = set()
        for item in _list_of_dicts(provides, "provides"):
            kind_ = item.get("type")
            if kind_ not in INTERFACE_TYPES:
                raise ValueError("Invalid provided interface type")
            entry = {"type": kind_, "name": _identifier(item.get("name"), "provides.name"),
                     "version": _interface_version(item.get("version"), "provides")}
            if (kind_, entry["name"]) in seen:
                raise ValueError("Duplicate provided interface")
            seen.add((kind_, entry["name"]))
            self.provides.append(entry)
        self.requires = []
        for item in _list_of_dicts(requires, "requires"):
            kind_ = item.get("type")
            if kind_ not in INTERFACE_TYPES:
                raise ValueError("Invalid required interface type")
            entry = {"type": kind_, "name": _identifier(item.get("name"), "requires.name"),
                     "min_version": _interface_version(item.get("min_version", "0.0"), "requires.min")}
            if "max_version" in item:
                entry["max_version"] = _interface_version(item["max_version"], "requires.max")
            self.requires.append(entry)

        self.api = None
        if api is not None:
            if not isinstance(api, dict):
                raise ValueError("Module api must be an object")
            routes = api.get("routes", [])
            if not isinstance(routes, list) or any(not isinstance(r, str) or not _ROUTE.fullmatch(r) or ".." in r for r in routes):
                raise ValueError("Invalid module api routes")
            self.api = {"namespace": _identifier(api.get("namespace"), "api.namespace"),
                        "version": _interface_version(api.get("version"), "api"), "routes": list(routes)}
        self.ui = None
        if ui is not None:
            if not isinstance(ui, dict):
                raise ValueError("Module ui must be an object")
            entry = ui.get("entry")
            if not isinstance(entry, str) or not entry or entry.startswith("/") or ".." in entry.split("/"):
                raise ValueError("Invalid module ui entry")
            navigation = _list_of_dicts(ui.get("navigation"), "ui.navigation")
            for item in navigation:
                _identifier(item.get("id"), "ui.navigation.id")
                # Optional menu details; CORE decides whether a route may be used (core/module/navigation.py).
                if not isinstance(item.get("label", ""), str) or not isinstance(item.get("admin_only", False), bool):
                    raise ValueError("Invalid module ui.navigation entry")
                if "route" in item and (not isinstance(item["route"], str) or not _ROUTE.fullmatch(item["route"])
                                        or ".." in item["route"]):
                    raise ValueError("Invalid module ui.navigation.route")
                if "group" in item:
                    _identifier(item["group"], "ui.navigation.group")
            files = ui.get("files", {})
            if not isinstance(files, dict) or any(
                    not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest) for path, digest in files.items()):
                raise ValueError("Module ui.files must map safe paths to sha256 digests")
            if files and entry not in files:
                raise ValueError("Module ui entry must be one of the signed ui.files")
            self.ui = {"entry": entry, "navigation": navigation, "files": dict(files)}

        permissions = permissions or []
        if not isinstance(permissions, list) or any(not isinstance(p, str) or not _PERMISSION.fullmatch(p) for p in permissions):
            raise ValueError("Invalid module permissions")
        self.permissions = list(permissions)

        self.secret_refs = []
        for item in _list_of_dicts(secret_refs, "secret_refs"):
            # Secrets live in the platform store; a manifest carries references only.
            if set(item) - {"name", "ref"}:
                raise ValueError("Module secret_refs may contain only name/ref; secret values are forbidden")
            ref = item.get("ref")
            if not isinstance(ref, str) or not _SECRET_REF.fullmatch(ref):
                raise ValueError("Module secret ref must be secret://<scope>/<name>")
            self.secret_refs.append({"name": _identifier(item.get("name"), "secret_refs.name"), "ref": ref})

        self.migrations = []
        ids = set()
        for item in _list_of_dicts(migrations, "migrations"):
            migration_id = _identifier(item.get("id"), "migrations.id")
            if migration_id in ids:
                raise ValueError("Duplicate module migration id")
            ids.add(migration_id)
            self.migrations.append({"id": migration_id, "version": self._validate_version(item.get("version"))})

        self.services = []
        names = set()
        for item in _list_of_dicts(services, "services"):
            service = _identifier(item.get("name"), "services.name")
            if service in names:
                raise ValueError("Duplicate module service")
            names.add(service)
            self.services.append({"name": service})

        health = dict(health or {})
        if set(health) - {"self_test", "timeout_seconds"}:
            raise ValueError("Unknown module health field")
        timeout = health.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ValueError("Invalid module health timeout")
        if "self_test" in health:
            _identifier(health["self_test"], "health.self_test")
        self.health = {**health, "timeout_seconds": timeout}

    @staticmethod
    def _validate_version(version: str) -> str:
        """Validate semver format A.B.C.D."""
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError(f"Invalid version format: {version}. Expected SemVer or legacy A.B.C.D")
        return version

    def core_compatible(self, core_version: str | None) -> bool:
        """True when the CORE runtime version lies in the declared range."""
        if not self.core_compatibility:
            return True
        if core_version is None:
            return False
        from packaging.version import Version
        current = Version(core_version)
        if "min" in self.core_compatibility and current < Version(self.core_compatibility["min"]):
            return False
        if "max" in self.core_compatibility and current > Version(self.core_compatibility["max"]):
            return False
        return True

    def to_dict(self) -> dict:
        """Convert manifest to dictionary for serialization.

        Legacy 1.x manifests serialize to exactly the legacy keys so existing
        inventory/protocol consumers see an unchanged shape.
        """
        data = {
            "name": self.name,
            "version": self.version,
            "capabilities": self.capabilities,
            "dependencies": self.dependencies,
            "resource_requirements": self.resource_requirements,
            "description": self.description,
        }
        if _schema_major(self.schema_version) >= 2:
            data.update(schema_version=self.schema_version, module_id=self.module_id, kind=self.kind,
                        core_compatibility=self.core_compatibility, placement=self.placement,
                        provides=self.provides, requires=self.requires, permissions=self.permissions,
                        secret_refs=self.secret_refs, migrations=self.migrations, services=self.services,
                        health=self.health)
            if self.api is not None:
                data["api"] = self.api
            if self.ui is not None:
                data["ui"] = self.ui
            for field in ("publisher", "provenance", "container"):
                if getattr(self, field) is not None:
                    data[field] = getattr(self, field)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ModuleManifest":
        """Create manifest from dictionary (package, inventory or bundled YAML)."""
        data = data.get("manifest", data)
        if "module" in data:
            data = data["module"]
        dependencies = data.get("dependencies")
        if dependencies is None:
            dependencies = data.get("depends_on", [])
        resources = data.get("resource_requirements")
        if resources is None and isinstance(data.get("resources"), dict):
            legacy = data["resources"]
            resources = {"min_vram_mb": legacy.get("min_vram_mb", 0), "min_cpu_cores": legacy.get("cpu_cores", 0),
                         "min_ram_mb": legacy.get("ram_mb", 0)}
        if resources is None and isinstance(data.get("runtime"), dict):
            rt = data["runtime"]
            resources = {"min_vram_mb": 0 if not rt.get("gpu") else rt.get("memory_mb", 0),
                         "min_cpu_cores": 1, "min_ram_mb": rt.get("memory_mb", 512)}
        placement = data.get("placement")
        if placement is None and isinstance(data.get("runtime"), dict):
            rt = data["runtime"]
            placement = {"target": "any", "scaling": "single_owner", "requires_gpu": rt.get("gpu", False)}
        migrations = data.get("migrations")
        if migrations is None and isinstance(data.get("database"), dict):
            db = data["database"]
            migrations_dir = db.get("migrations_dir", "migrations")
            migrations = [{"id": "m001_initial", "version": "1.0.0"}]
        health = data.get("health")
        if health is None and isinstance(data.get("health_check"), dict):
            hc = data["health_check"]
            health = {"self_test": "health", "timeout_seconds": hc.get("timeout_sec", 30)}
        capabilities = data.get("capabilities")
        if capabilities is None:
            capabilities = data.get("provides_capabilities", [])
        requires = data.get("requires")
        if requires is None and isinstance(data.get("requires_capabilities"), list):
            requires = [{"type": "capability", "name": cap, "min_version": "0.0"} for cap in data["requires_capabilities"]]
        container = data.get("container")
        if "container" not in data and data.get("kind") == "application" and isinstance(data.get("runtime"), dict):
            container = {"image": f"registry.example/vertep/{data['name']}@sha256:{'0' * 64}", "port": 8080}
        api = data.get("api")
        if api is None and data.get("namespace"):
            api = {"namespace": data["namespace"], "version": "1.0", "routes": [f"/{data['namespace']}", f"/{data['namespace']}/{{id}}"]}
        extended = {key: data[key] for key in ("module_id", "kind", "core_compatibility", "provides",
                                                "ui", "permissions", "secret_refs", "services",
                                                "publisher", "provenance") if key in data}
        if requires is not None:
            extended["requires"] = requires
        if api is not None:
            extended["api"] = api
        if placement is not None:
            extended["placement"] = placement
        if migrations is not None:
            extended["migrations"] = migrations
        if health is not None:
            extended["health"] = health
        if container is not None:
            extended["container"] = container
        return cls(
            name=data["name"],
            version=data["version"],
            capabilities=capabilities or [],
            dependencies=dependencies or [],
            resource_requirements=resources or {},
            description=data.get("description", "") or "",
            schema_version=data.get("schema_version"),
            **extended,
        )

    def __repr__(self) -> str:
        return f"ModuleManifest(name={self.name}, version={self.version})"
