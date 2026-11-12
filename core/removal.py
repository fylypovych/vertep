"""Local installation removal. No shell evaluation; all mutations use a confirmed inventory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager

DATA = {"config", "tls", "data", "storage", "models", "logs", "backups", ".env", ".update-backup.key"}
UNIT_NAMES = (
    "vertep-core.service", "vertep-worker.service", "vertep-comfyui.service",
    "vertep-update.service", "vertep-update.path", "vertep-update-check.service", "vertep-update.timer",
    "vertep-worker-update@.service", "vertep-worker-update.path",
    "vertep-deployment.service", "vertep-deployment.path", "vertep-watchdog.service",
    "vertep-watchdog.timer", "vertep-startup-recovery.service",
    "vertep-host-health.service", "vertep-host-health.timer",
)
JOURNAL_BASE = Path("/var/lib/vertep-remove")


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def digest(path):
    if path.is_symlink():
        return "link:" + os.readlink(path)
    value=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def fence(root):
    """Same local OS lock as UpdateLease; independent recovery script needs no installed imports."""
    path = root / "config/update" / ("update.lock.guard" if os.name == "nt" else "update.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); stream.write("\0"); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temp.is_symlink():
        raise RuntimeError("Symlink замість службового файла")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def safe_root(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("Потрібен абсолютний installation root без symlink")
    path = path.resolve()
    if path in {Path("/"), Path("/opt"), Path("/usr"), Path("/etc"), Path("/var"), Path.home()} or len(path.parts) < 3:
        raise RuntimeError("Небезпечний installation root")
    if any(path.is_relative_to(Path(prefix)) for prefix in ["/etc", "/usr", "/proc", "/sys", "/dev", "/bin", "/sbin", "/var/lib/docker"]):
        raise RuntimeError("Installation root перетинається із системним каталогом")
    return path


class Host:
    def __init__(self, root, runner=subprocess.run, system=Path("/")):
        self.root = safe_root(root)
        self.system = system.resolve()
        self.runner = runner

    def command(self, args):
        result = self.runner(args, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode:
            # Do not reflect command output, env values or credentials into receipts.
            raise RuntimeError("Команда не виконана: " + args[0] + " " + args[1])
        return result.stdout

    def docker(self, *args):
        return self.command(["docker", *args])

    def inventory(self):
        ids = self.docker("ps", "-aq").split()
        return json.loads(self.docker("inspect", *ids)) if ids else []

    def unit_path(self, name):
        return self.system / "etc/systemd/system" / name

    def cli_path(self):
        return self.system / "usr/local/bin/vertep"


def record(host):
    """Record only release-owned files and units pointing at this root, not shared host packages."""
    root = host.root
    path = root / "config/removal-ownership.json"
    previous = read(path, {})
    active=root/"current"
    manifest = read(active/"manifest.json", {}) if active.is_dir() and active.resolve().is_relative_to(root/"releases") else read(root / "manifest.json", {})
    if not isinstance(manifest.get("files"), dict):
        raise RuntimeError("Немає manifest файлів runtime; належність ПЗ не доведена")
    # Files of an earlier, already removed runtime are no longer evidence of ownership.
    files = {name: value for name, value in previous.get("files", {}).items()
             if (root / name).exists() or (root / name).is_symlink()}
    for name in manifest["files"]:
        relative = Path(name)
        candidate = root / relative
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Небезпечний шлях manifest")
        if relative.parts[0] in DATA:
            continue
        if candidate.is_file() and candidate.resolve().is_relative_to(root):
            files[name] = digest(candidate)
    # Files copied/generated by bootstrap outside the signed manifest path layout.
    for name in ["manifest.json", "SHA256SUMS", "images.json", "VERSION", "docker-compose.yml",
                 "docker-compose.amd.yml", "docker-compose.nvidia.yml", ".activation.json", ".vertep-last-good"]:
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            files[name] = digest(candidate)
    # Immutable release trees are software; record each file, never recursively delete an uninspected tree.
    for directory in [root / "releases"]:
        if directory.is_dir() and not directory.is_symlink():
            for candidate in directory.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink() and candidate.resolve().is_relative_to(root):
                    files[candidate.relative_to(root).as_posix()] = digest(candidate)
    current = root / "current"
    if current.is_symlink() and current.resolve().is_relative_to(root / "releases"):
        files["current"] = digest(current)
    units = dict(previous.get("units", {}))
    for name in UNIT_NAMES:
        unit = host.unit_path(name)
        templates=[root/name,root/"installer"/name]
        if unit.is_file() and not unit.is_symlink():
            content=unit.read_text(encoding="utf-8")
            if str(root) in content and any(template.is_file() and
                    content==template.read_text(encoding="utf-8").replace("@VERTEP_ROOT@",str(root)) for template in templates):
                units[name] = digest(unit)
    # Timer/path templates may not contain a root; require exact bundled content.
    for name in UNIT_NAMES:
        installed, bundled = host.unit_path(name), root / name
        service = name.rsplit(".", 1)[0] + ".service"
        if service in units and installed.is_file() and bundled.is_file() and not installed.is_symlink() and digest(installed) == digest(bundled):
            units[name] = digest(installed)
    owned = [item for item in host.inventory() if owned_container(item, root)]
    # Image provenance: a snapshot is valid only before Vertep created any container for
    # this root. Those images existed before Vertep and are never removed by it.
    provenance = previous.get("image_provenance")
    if not previous and not owned:
        provenance = {"schema": 1, "preexisting": sorted(set(host.docker("image", "ls", "-q", "--no-trunc").split()))}
    resources = previous.get("resources", {"images": [], "volumes": [], "networks": []})
    resources["images"] = sorted(set(resources["images"]) | {item["Image"] for item in owned})
    resources["volumes"] = sorted(set(resources["volumes"]) | {m["Name"] for item in owned for m in item.get("Mounts", []) if m.get("Type") == "volume"})
    for name in sorted({name for item in owned for name in (item.get("NetworkSettings") or {}).get("Networks", {})}):
        value = json.loads(host.docker("network", "inspect", name))[0]
        if (value.get("Labels") or {}).get("com.docker.compose.network"):
            resources["networks"] = sorted(set(resources["networks"]) | {value["Id"]})
    value = {"schema": 1, "root": str(root), "installation_id": previous.get("installation_id") or uuid.uuid4().hex,
             "files": files, "units": units, "resources": resources, "image_provenance": provenance,
             "packages": {}, "package_provenance": "unknown; shared dependencies are retained"}
    atomic(path, value)
    return value


def owned_container(item, root):
    labels = (item.get("Config") or {}).get("Labels") or {}
    directory = labels.get("com.docker.compose.project.working_dir", "")
    configs = labels.get("com.docker.compose.project.config_files", "")
    return (directory and Path(directory).resolve().is_relative_to(root) and configs and
            all(Path(p).resolve().is_relative_to(root) for p in configs.split(",")))


def volume_exclusive(value, foreign_volumes, foreign):
    if value.get("Name") in foreign_volumes or not (value.get("Labels") or {}).get("com.docker.compose.volume"):
        return False
    mountpoint=value.get("Mountpoint")
    if mountpoint:
        location=Path(mountpoint)
        for item in foreign:
            for mount in item.get("Mounts",[]):
                if mount.get("Type")=="bind":
                    source=Path(mount["Source"])
                    if source==location or source.is_relative_to(location) or location.is_relative_to(source):
                        return False
    return True


def signed_image_references(root):
    """repository@digest of images from the signed runtime locks of this root."""
    references = set()
    for path in [root / "images.json", *sorted((root / "releases").glob("*/images.json"))]:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            lock = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for item in (lock.values() if isinstance(lock, dict) else []):
            if isinstance(item, dict) and isinstance(item.get("reference"), str) and isinstance(item.get("digest"), str):
                reference = item["reference"]
                repository = reference.rsplit(":", 1)[0] if ":" in reference.rsplit("/", 1)[-1] else reference
                references.add(repository + "@" + item["digest"])
    return references


def image_owned(host, ident, provenance, signed):
    """Remove only images Vertep brought to this host; unknown origin is retained."""
    if isinstance(provenance, dict) and isinstance(provenance.get("preexisting"), list):
        if ident in provenance["preexisting"]:
            return False, "Образ існував до інсталяції Vertep: " + ident
        return True, ""
    value = json.loads(host.docker("image", "inspect", ident))[0]
    if signed & set(value.get("RepoDigests") or []):
        return True, ""
    return False, "Походження образу не зафіксоване: " + ident


def plan(host, purge=False):
    root = host.root
    ledger = read(root / "config/removal-ownership.json")
    if not isinstance(ledger, dict) or ledger.get("schema") != 1 or ledger.get("root") != str(root):
        raise RuntimeError("Немає перевіреного ownership inventory. Стару/часткову інсталяцію слід окремо інвентаризувати; видалення заблоковано")
    result = {"schema": 1, "root": str(root), "installation_id": ledger["installation_id"],
              "purge_data": purge, "files": {}, "units": {}, "containers": [], "networks": [],
              "images": [], "volumes": [], "data": [], "retained": [], "blocked": []}
    software = dict(ledger["files"])
    for name in ledger["files"]:
        source = root / name
        if source.suffix == ".py" and not source.parent.is_symlink():
            for cached in (source.parent / "__pycache__").glob(source.stem + ".*.pyc"):
                if not cached.is_symlink() and cached.resolve().is_relative_to(root):
                    software[cached.relative_to(root).as_posix()] = digest(cached)
    for name, expected in software.items():
        relative = Path(name); path = root / relative
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] in DATA:
            raise RuntimeError("Некоректний software inventory")
        if not path.parent.resolve().is_relative_to(root):
            raise RuntimeError("Шлях виходить за installation root")
        if path.exists() or path.is_symlink():
            if digest(path) != expected:
                result["blocked"].append("Змінений файл: " + name)
            else:
                result["files"][name] = expected
    for name, expected in ledger["units"].items():
        if name not in UNIT_NAMES:
            raise RuntimeError("Невідомий systemd unit")
        path = host.unit_path(name)
        if path.exists():
            if path.is_symlink() or digest(path) != expected:
                result["blocked"].append("Змінений unit: " + name)
            else:
                result["units"][name] = expected
    for name in UNIT_NAMES:
        unit=host.unit_path(name)
        if name not in ledger["units"] and unit.is_file() and str(root) in unit.read_text(encoding="utf-8"):
            result["blocked"].append("Unit відсутній в ownership inventory: "+name)
        dropins=unit.with_name(unit.name+".d")
        if name in ledger["units"] and dropins.exists():
            result["blocked"].append("Systemd drop-ins потребують окремої перевірки: "+name)
    cli = host.cli_path()
    result["cli"] = bool(cli.is_symlink() and cli.resolve() == root / "scripts/vertep")
    items = host.inventory()  # Docker unavailable is not evidence that its resources are absent.
    ours = [item for item in items if owned_container(item, root)]
    foreign = [item for item in items if item not in ours]
    result["foreign_containers"] = {item["Id"]: {"image": item.get("Image"), "running": (item.get("State") or {}).get("Running")}
                                    for item in foreign}
    for item in ours:
        result["containers"].append(item["Id"])
    own_ids = set(result["containers"])
    foreign_images = {item.get("Image") for item in foreign}
    known = ledger.get("resources", {})
    available_images = set(host.docker("image", "ls", "-q", "--no-trunc").split())
    candidates = sorted(({item["Image"] for item in ours} | set(known.get("images", []))) & (available_images - foreign_images))
    signed = signed_image_references(root)
    for ident in candidates:
        owned, reason = image_owned(host, ident, ledger.get("image_provenance"), signed)
        if owned:
            result["images"].append(ident)
        else:
            result["retained"].append(reason)
    foreign_mounts = {m.get("Source") for item in foreign for m in item.get("Mounts", [])}
    for item in foreign:
        for mount in item.get("Mounts",[]):
            if mount.get("Type")=="bind":
                source=Path(mount["Source"]).resolve()
                if source.is_relative_to(root) or root.is_relative_to(source):
                    result["blocked"].append("Сторонній bind mount перетинає installation root: "+str(source))
    foreign_volumes = {m.get("Name") for item in foreign for m in item.get("Mounts", []) if m.get("Type") == "volume"}
    # Networks require all attached containers to belong to this exact root; project name alone is insufficient.
    available_networks = set(host.docker("network", "ls", "-q", "--no-trunc").split())
    names = {name for item in ours for name in (item.get("NetworkSettings") or {}).get("Networks", {})}
    names.update(set(known.get("networks", [])) & available_networks)
    for name in sorted(names):
        network = json.loads(host.docker("network", "inspect", name))[0]
        labels = network.get("Labels") or {}
        if labels.get("com.docker.compose.network") and set(network.get("Containers") or {}) <= own_ids:
            result["networks"].append(network["Id"])
        else:
            result["retained"].append("Спільна/невизначена network: " + name)
    for item in ours:
        for mount in item.get("Mounts", []):
            if mount.get("Type") == "volume":
                name = mount["Name"]
                value = json.loads(host.docker("volume", "inspect", name))[0]
                labels = value.get("Labels") or {}
                if purge and volume_exclusive(value,foreign_volumes,foreign):
                    result["volumes"].append(name)
                else:
                    result["retained"].append("Volume: " + name)
            elif mount.get("Type") == "bind":
                source = Path(mount["Source"]).resolve()
                if source.is_relative_to(root) and mount["Source"] in foreign_mounts:
                    result["blocked"].append("Сторонній контейнер використовує " + str(source))
    available_volumes = set(host.docker("volume", "ls", "-q").split())
    for name in set(known.get("volumes", [])) & available_volumes:
        value = json.loads(host.docker("volume", "inspect", name))[0]
        if purge and volume_exclusive(value,foreign_volumes,foreign):
            result["volumes"].append(name)
        else:
            result["retained"].append("Volume: " + name)
    # Inventory all data entries, with content fingerprints so a confirmation cannot silently grow.
    for name in sorted(DATA):
        path = root / name
        if path.is_symlink():
            result["blocked"].append("Data symlink потребує окремого рішення: " + name)
            continue
        if not path.exists():
            continue
        if purge:
            paths = sorted(path.rglob("*")) if path.is_dir() else [path]
            for entry in paths:
                if entry.relative_to(root).as_posix() in {"config/update/update.lock", "config/update/update.lock.guard", "config/removal-in-progress.json"}:
                    continue
                if entry.is_symlink() or not entry.resolve().is_relative_to(root):
                    raise RuntimeError("Symlink у даних; автоматичне видалення заблоковано")
                if entry.is_file():
                    result["data"].append({"path": str(entry.relative_to(root)), "sha256": digest(entry)})
        else:
            result["retained"].append(str(path))
    result["volumes"] = sorted(set(result["volumes"]))
    result["retained"] = sorted(set(result["retained"]))
    result["retained"].append("Спільні пакети, apt repositories, Docker/SSH/UFW settings: походження/використання не доведено")
    canonical = json.dumps(result, sort_keys=True).encode()
    result["plan_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def activity(host):
    # Running executors and queued requests fence removal before any stop/delete.
    for name in ["vertep-update.service", "vertep-deployment.service", "vertep-startup-recovery.service"]:
        output = host.command(["systemctl", "show", name, "--property=ActiveState", "--value"]).strip()
        if output in {"active", "activating", "deactivating"}:
            raise RuntimeError("Активний системний executor: " + name)
    if (host.root / "config/deployment-request.json").exists() or list((host.root / "config/update/requests").glob("*.json")):
        raise RuntimeError("Є запити deployment/update; спочатку завершіть або окремо скасуйте їх")
    # Active jobs require live control evidence. No API credentials are sourced or printed.
    items = [item for item in host.inventory() if owned_container(item, host.root) and (item.get("State") or {}).get("Running")]
    if not items:
        # Partial/stopped container installs have no local runtime to kill.
        for name in ["vertep-core.service", "vertep-worker.service", "vertep-comfyui.service"]:
            if host.unit_path(name).exists() and host.command(["systemctl", "show", name, "--property=ActiveState", "--value"]).strip() in {"active", "activating"}:
                raise RuntimeError("Legacy runtime активний; спочатку завершіть задачі й штатно зупиніть " + name)
        return
    core = next((item for item in items if ((item.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service") == "core"), None)
    if core is None:
        raise RuntimeError("Worker runtime активний; спочатку завершіть задачі й штатно зупиніть локальні контейнери")
    script = ("import json,os,urllib.request; "
              "r=urllib.request.Request('http://127.0.0.1:8080/api/system/update/readiness',headers={'X-Vertep-Internal-Key':os.environ['INTERNAL_API_KEY']}); "
              "v=json.load(urllib.request.urlopen(r,timeout=10)); "
              "print(json.dumps({k:v.get(k) for k in ['ready','active_jobs','busy_workers','inflight','queue_paused']}))")
    evidence = json.loads(host.docker("exec", core["Id"], "python3", "-c", script))
    if (evidence.get("ready") is not True or evidence.get("queue_paused") is not True or evidence.get("active_jobs") != []
            or evidence.get("busy_workers") != [] or type(evidence.get("inflight")) is not int or evidence["inflight"] != 0):
        raise RuntimeError("Є активні задачі або dispatch не призупинено. Спочатку ввімкніть maintenance і завершіть drain через штатний CORE")


def execute(host, selected, journal_dir, resume=False):
    config=host.root/"config"
    if config.is_symlink() or not config.resolve().is_relative_to(host.root):
        raise RuntimeError("Небезпечний шлях конфігурації")
    update=config/"update"
    if update.is_symlink() or not update.resolve().is_relative_to(host.root):
        raise RuntimeError("Небезпечний шлях update fence")
    if host.system==Path("/").resolve():
        for directory in [journal_dir.parent,journal_dir]:
            if directory.exists() and (directory.is_symlink() or directory.stat().st_uid!=0 or directory.stat().st_mode & 0o022):
                raise RuntimeError("Небезпечний каталог recovery journal")
    with fence(host.root):
        activity(host)
        if not resume:
            # Acquiring the fence creates a lock file: not part of the data deletion scope.
            current = plan(host, selected["purge_data"])
            if current != selected:
                raise RuntimeError("Перелік змінився; повторіть dry-run і підтвердження")
        atomic(host.root / "config/removal-in-progress.json", {"plan_sha256": selected["plan_sha256"]})
        # The marker closes admission across CORE processes; recheck drained runtime after publication.
        activity(host)
        state = _execute(host, selected, journal_dir)
    if selected["purge_data"]:
        # After the fence is released its lock file is no longer needed by any executor.
        state = _finish_purge(host, journal_dir, state)
    return state


def _finish_purge(host, journal_dir, state):
    """Remove the emptied installation layout; unknown content is kept and reported."""
    root = host.root
    for name in ["config/update/update.lock", "config/update/update.lock.guard"]:
        target = root / name
        if target.is_file() and not target.is_symlink() and target.parent.resolve().is_relative_to(root):
            try:
                target.unlink()
            except OSError:
                pass
    for name in sorted(DATA):
        directory = root / name
        if directory.is_dir() and not directory.is_symlink():
            for child in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if child.is_dir() and not child.is_symlink():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                directory.rmdir()
            except OSError:
                pass
    leftovers = sorted(str(item) for item in root.iterdir()) if root.is_dir() else []
    if leftovers:
        state["retained_unknown"] = sorted(set(state.get("retained_unknown", [])) | set(leftovers))
    elif root.is_dir():
        root.rmdir()
    state["state"] = "PARTIAL" if state.get("retained_unknown") else "SUCCEEDED"
    atomic(journal_dir.resolve() / "state.json", state)
    return state


def find_journal(base, root):
    """The one unfinished journal of this installation root, independent of its directory key."""
    found = []
    for path in sorted(base.glob("*/state.json")) if base.is_dir() else []:
        if path.is_symlink() or path.parent.is_symlink():
            continue
        state = read(path, {})
        if isinstance(state, dict) and (state.get("plan") or {}).get("root") == str(root) and state.get("state") != "SUCCEEDED":
            found.append(path.parent)
    if not found:
        raise RuntimeError("Немає незавершеного журналу видалення для " + str(root))
    if len(found) > 1:
        raise RuntimeError("Кілька незавершених журналів; запустіть потрібний збережений executor: "
                           + ", ".join(str(item / "remove.py") for item in found))
    return found[0]


def _execute(host, selected, journal_dir):
    if selected["blocked"]:
        raise RuntimeError("Є ресурси з непідтвердженою належністю: " + "; ".join(selected["blocked"]))
    if any(p.is_symlink() for p in [journal_dir,*journal_dir.parents]):
        raise RuntimeError("Symlink у шляху журналу видалення")
    journal_dir = journal_dir.resolve()
    if journal_dir.is_relative_to(host.root):
        raise RuntimeError("Журнал видалення має бути поза installation root")
    journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(journal_dir, 0o700)
    path = journal_dir / "state.json"
    if path.is_symlink() or (journal_dir/"remove.py").is_symlink():
        raise RuntimeError("Symlink у журналі видалення")
    state = read(path, {"state": "RUNNING", "plan": selected, "done": []})
    if state["plan"] != selected:
        raise RuntimeError("Журнал належить іншому переліку видалення")
    state["state"]="RUNNING"
    state.pop("retained_unknown",None)
    state.pop("error",None)
    # A self-contained recovery executor survives removal of the installation and CLI.
    if Path(__file__).resolve() != (journal_dir / "remove.py").resolve():
        shutil.copyfile(Path(__file__), journal_dir / "remove.py")
    os.chmod(journal_dir / "remove.py", 0o700)
    def step(key, action):
        if key in state["done"]:
            return
        action()
        state["done"].append(key)
        atomic(path, state)
    atomic(path, state)
    try:
        for name in sorted(selected["units"], key=lambda n: not n.endswith((".timer", ".path"))):
            def stop(n=name):
                target=host.unit_path(n)
                if target.exists():
                    if target.is_symlink() or digest(target) != selected["units"][n]:
                        raise RuntimeError("Unit змінився після підтвердження")
                    host.command(["systemctl", "disable", "--now", n])
            step("stop:" + name, stop)
        for ident in selected["containers"]:
            def remove_container(i=ident):
                current=next((item for item in host.inventory() if item["Id"]==i),None)
                if current is not None:
                    if not owned_container(current,host.root):raise RuntimeError("Змінилася належність контейнера")
                    host.docker("rm", "-f", i)
            step("container:" + ident, remove_container)
        for kind, plural in [("network", "networks"), ("volume", "volumes"), ("image", "images")]:
            for ident in selected[plural]:
                def remove_resource(k=kind,i=ident):
                    inventory=host.docker(k,"ls","-q",*(["--no-trunc"] if k!="volume" else [])).split()
                    if i in inventory:
                        if k=="volume":
                            containers=host.inventory()
                            foreign=[item for item in containers if not owned_container(item,host.root)]
                            names={m.get("Name") for item in foreign for m in item.get("Mounts",[]) if m.get("Type")=="volume"}
                            value=json.loads(host.docker("volume","inspect",i))[0]
                            if not volume_exclusive(value,names,foreign):
                                raise RuntimeError("Volume став спільним після підтвердження")
                        host.docker(k,"rm",i)
                step(kind + ":" + ident, remove_resource)
        for name, expected in selected["units"].items():
            def remove_unit(n=name, expected=expected):
                target = host.unit_path(n)
                if target.exists():
                    if target.is_symlink() or digest(target) != expected:
                        raise RuntimeError("Unit змінився після підтвердження")
                    target.unlink()
            step("unit:" + name, remove_unit)
        step("reload", lambda: host.command(["systemctl", "daemon-reload"]))
        for name, expected in selected["files"].items():
            def remove_file(n=name, e=expected):
                target = host.root / n
                if not target.parent.resolve().is_relative_to(host.root):
                    raise RuntimeError("Змінився шлях після підтвердження")
                if target.exists() or target.is_symlink():
                    if digest(target) != e:
                        raise RuntimeError("Файл змінився після підтвердження: " + n)
                    target.unlink()
            step("file:" + name, remove_file)
        for item in selected["data"]:
            def remove_data(item=item):
                target = host.root / item["path"]
                if target.exists():
                    if target.is_symlink() or not target.resolve().is_relative_to(host.root) or digest(target) != item["sha256"]:
                        raise RuntimeError("Дані змінилися після підтвердження")
                    target.unlink()
            step("data:" + item["path"], remove_data)
        if selected["cli"]:
            def unlink_cli():
                target = host.cli_path()
                if target.is_symlink() and os.readlink(target) == str(host.root / "scripts/vertep"):
                    target.unlink()
            step("cli", unlink_cli)
        remaining=host.inventory()
        if any(owned_container(item,host.root) for item in remaining):
            raise RuntimeError("Залишилися контейнери інсталяції")
        by_id={item["Id"]:item for item in remaining}
        for ident,evidence in selected["foreign_containers"].items():
            item=by_id.get(ident)
            if item is None or item.get("Image")!=evidence["image"] or (item.get("State") or {}).get("Running")!=evidence["running"]:
                raise RuntimeError("Змінився стан стороннього контейнера; потрібна перевірка")
        for name in selected["units"]:
            if host.unit_path(name).exists():raise RuntimeError("Залишився systemd unit")
        # Remove only empty software directories, never recursively delete unknown contents.
        for name in ["core", "scripts", "db", "runtime", "releases", "monitoring", "installer"]:
            directory=host.root/name
            if directory.is_dir() and not directory.is_symlink():
                for child in sorted(directory.rglob("*"),key=lambda p:len(p.parts),reverse=True):
                    if child.is_dir() and not child.is_symlink():
                        try: child.rmdir()
                        except OSError: pass
                try: directory.rmdir()
                except OSError: state.setdefault("retained_unknown",[]).append(str(directory))
        (host.root / "config/removal-in-progress.json").unlink(missing_ok=True)
        state["state"] = "PARTIAL" if state.get("retained_unknown") else "SUCCEEDED"
        atomic(path, state)
        return state
    except Exception as error:
        state["state"] = "FAILED"
        state["error"] = str(error)
        atomic(path, state)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Видалення лише підтверджених ресурсів інсталяції Vertep")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--record", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--confirm", default="", help="SHA256 переліку, показаного dry-run")
    parser.add_argument("--resume", action="store_true")
    args=parser.parse_args(argv)
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("Потрібні Linux та права root")
        host=Host(args.root)
        for protected in [host.root,host.root/"config"]:
            if protected.is_symlink() or not protected.is_dir() or protected.stat().st_uid != 0 or protected.stat().st_mode & 0o022:
                raise RuntimeError("Installation root/config мають належати root і не дозволяти сторонній запис")
        if args.record:
            record(host); return 0
        ledger=read(host.root/"config/removal-ownership.json",{})
        key=hashlib.sha256((str(host.root)+str(ledger.get("installation_id",""))).encode()).hexdigest()[:24]
        journal=JOURNAL_BASE/key
        if args.resume:
            recovery=Path(__file__).resolve().parent
            if (recovery/"state.json").exists() and recovery.is_relative_to(JOURNAL_BASE):
                journal=recovery
            else:
                journal=find_journal(JOURNAL_BASE,host.root)
            previous=read(journal/"state.json")
            if not previous: raise RuntimeError("Немає журналу для продовження")
            selected=previous["plan"]
            if selected["root"] != str(host.root):raise RuntimeError("Журнал іншої інсталяції")
        else:
            selected=plan(host,args.purge_data)
            journal=journal.with_name(journal.name+"-"+selected["plan_sha256"][:16])
        print(json.dumps(selected,ensure_ascii=False,indent=2))
        if args.dry_run: return 0 if not selected["blocked"] else 2
        if args.confirm != selected["plan_sha256"]:
            answer=input("Для підтвердження введіть повний SHA256 цього переліку: ") if sys.stdin.isatty() else ""
            if answer != selected["plan_sha256"]: raise RuntimeError("Видалення не підтверджене")
        result=execute(host,selected,journal,resume=args.resume)
        print("Результат видалення: " + result["state"] + "; журнал: " + str(journal/"state.json"))
        return 0 if result["state"] == "SUCCEEDED" else 2
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print("Видалення заблоковано або не завершено: " + str(error),file=sys.stderr)
        if "journal" in locals() and (journal/"state.json").is_file():
            print("Збережений executor для продовження: "+str(journal/"remove.py"),file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
