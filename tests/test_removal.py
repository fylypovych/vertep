"""Removal safety with disposable filesystem and simulated OS/Docker, never the host runtime."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import removal as r


class Runtime:
    def __init__(self, root):
        self.calls=[]
        self.containers={
            "own": {"Id":"own","Image":"sha256:own","Config":{"Labels":{
                "com.docker.compose.project":"vertep", "com.docker.compose.service":"core",
                "com.docker.compose.project.working_dir":str(root),
                "com.docker.compose.project.config_files":str(root/"docker-compose.yml")}},
                "State":{"Running":True},"Mounts":[{"Type":"volume","Name":"data"}],
                "NetworkSettings":{"Networks":{"private":{}}}},
            "foreign": {"Id":"foreign","Image":"sha256:foreign","Config":{"Labels":{
                "com.docker.compose.project":"vertep", # same name is not ownership
                "com.docker.compose.project.working_dir":str(root.parent/"other"),
                "com.docker.compose.project.config_files":str(root.parent/"other/docker-compose.yml")}},
                "State":{"Running":True},"Mounts":[],"NetworkSettings":{"Networks":{}}},
        }
        self.images={"sha256:own","sha256:foreign"}
        self.digests={}
        self.networks={"private":{"Id":"net-own","Labels":{"com.docker.compose.network":"default"},"Containers":{"own":{}}}}
        self.volumes={"data":{"Name":"data","Labels":{"com.docker.compose.volume":"postgres-data"}}}
        self.ready={"ready":True,"queue_paused":True,"active_jobs":[],"busy_workers":[],"inflight":0}
        self.states={}
        self.fail=None

    def run(self,args,**kwargs):
        self.calls.append(args)
        if self.fail and self.fail(args):raise RuntimeError("injected interruption")
        output=""
        if args[0]=="systemctl":
            if args[1]=="show":output=self.states.get(args[2],"inactive")
        elif args[1]=="ps":output="\n".join(self.containers)
        elif args[1]=="inspect":output=json.dumps([self.containers[x] for x in args[2:]])
        elif args[1]=="exec":output=json.dumps(self.ready)
        elif args[1]=="rm":
            ident=args[-1];self.containers.pop(ident)
            for network in self.networks.values():network["Containers"].pop(ident,None)
        elif args[1] in {"image","network","volume"}:
            kind,verb=args[1:3]
            data={"image":self.images,"network":self.networks,"volume":self.volumes}[kind]
            if verb=="ls":
                output="\n".join(item["Id"] for item in data.values()) if kind=="network" else "\n".join(data)
            elif verb=="inspect" and kind=="image":
                output=json.dumps([{"Id":args[3],"RepoDigests":self.digests.get(args[3],[])}])
            elif verb=="inspect":
                key=args[3]
                if kind=="network" and key not in data:key=next(k for k,v in data.items() if v["Id"]==key)
                output=json.dumps([data[key]])
            elif verb=="rm":
                ident=args[3]
                if kind=="image":data.remove(ident)
                elif kind=="network":data.pop(next(k for k,v in data.items() if v["Id"]==ident))
                else:data.pop(ident)
        return SimpleNamespace(returncode=0,stdout=output,stderr="")


@pytest.fixture
def install(tmp_path):
    root=tmp_path/"custom-install"
    (root/"scripts").mkdir(parents=True)
    (root/"scripts/vertep").write_text("runtime")
    (root/"config").mkdir()
    (root/"config/secrets.enc.json").write_text("secret")
    (root/"storage").mkdir()
    (root/"storage/video.mp4").write_bytes(b"user data")
    (root/"docker-compose.yml").write_text("services: {}")
    (root/"manifest.json").write_text(json.dumps({"files":{"scripts/vertep":{},"docker-compose.yml":{},"vertep-core.service":{}}}))
    system=tmp_path/"system"
    units=system/"etc/systemd/system";units.mkdir(parents=True)
    (units/"vertep-core.service").write_text("ExecStart="+str(root/"scripts/vertep"),encoding="utf-8")
    (root/"vertep-core.service").write_text(("ExecStart="+str(root/"scripts/vertep")).replace(str(root),"@VERTEP_ROOT@"),encoding="utf-8")
    runtime=Runtime(root)
    host=r.Host(root,runtime.run,system)
    # Bootstrap records ownership before its first pull, then again after start.
    own=runtime.containers.pop("own");runtime.images.discard("sha256:own")
    r.record(host)
    runtime.containers["own"]=own;runtime.images.add("sha256:own")
    r.record(host)
    runtime.calls.clear()
    return host,runtime,tmp_path/"journal"


def test_default_removes_software_preserves_data_and_other_install(install):
    host,runtime,journal=install
    foreign=copy.deepcopy(runtime.containers["foreign"])
    selected=r.plan(host)
    assert selected["containers"]==["own"]
    assert selected["data"]==[] and selected["volumes"]==[]
    assert not any("rm" in call for call in runtime.calls)
    result=r.execute(host,selected,journal)
    assert result["state"]=="SUCCEEDED"
    assert runtime.containers=={"foreign":foreign}
    assert runtime.images=={"sha256:foreign"}
    assert runtime.volumes.get("data")
    assert (host.root/"storage/video.mp4").read_bytes()==b"user data"
    assert (host.root/"config/secrets.enc.json").read_text()=="secret"
    assert not (host.root/"scripts/vertep").exists()
    assert (journal/"remove.py").exists()


def test_explicit_purge_only_confirmed_files_and_volume(install):
    host,runtime,journal=install
    selected=r.plan(host,True)
    assert selected["volumes"]==["data"]
    assert r.execute(host,selected,journal)["state"]=="SUCCEEDED"
    assert not (host.root/"storage/video.mp4").exists()
    assert not (host.root/"config/secrets.enc.json").exists()
    assert not runtime.volumes
    assert "foreign" in runtime.containers


@pytest.mark.parametrize("fault",["jobs","inflight","dispatch","worker","executor","queued"])
def test_active_work_refuses_before_any_stop_or_delete(install,fault):
    host,runtime,journal=install
    selected=r.plan(host)
    if fault=="jobs":runtime.ready["active_jobs"]=["job"]
    elif fault=="inflight":runtime.ready["inflight"]=1
    elif fault=="dispatch":runtime.ready["queue_paused"]=False
    elif fault=="worker":runtime.ready["busy_workers"]=["worker"]
    elif fault=="executor":runtime.states["vertep-update.service"]="active"
    else:(host.root/"config/deployment-request.json").write_text("{}")
    with pytest.raises(RuntimeError):r.execute(host,selected,journal)
    assert "own" in runtime.containers
    assert not any(call[1]=="disable" or "rm" in call for call in runtime.calls)


def test_shared_images_networks_and_volumes_are_retained(install):
    host,runtime,journal=install
    runtime.containers["foreign"]["Image"]="sha256:own"
    runtime.containers["foreign"]["Mounts"]=[{"Type":"volume","Name":"data"}]
    runtime.networks["private"]["Containers"]["foreign"]={}
    selected=r.plan(host,True)
    assert not selected["images"] and not selected["networks"] and not selected["volumes"]
    r.execute(host,selected,journal)
    assert runtime.images=={"sha256:own","sha256:foreign"}
    assert runtime.volumes and runtime.networks


@pytest.mark.parametrize("kind",["file","unit","new-data"])
def test_changed_scope_requires_new_confirmation(install,kind):
    host,runtime,journal=install
    selected=r.plan(host,True)
    if kind=="file":(host.root/"scripts/vertep").write_text("changed")
    elif kind=="unit":host.unit_path("vertep-core.service").write_text("some other service")
    else:(host.root/"storage/new.txt").write_text("not confirmed")
    with pytest.raises(RuntimeError):r.execute(host,selected,journal)
    assert "own" in runtime.containers


def test_interrupted_removal_resumes_without_repeating_completed_steps(install):
    host,runtime,journal=install
    selected=r.plan(host)
    runtime.fail=lambda args:args[1:3]==["image","rm"]
    with pytest.raises(RuntimeError,match="interruption"):r.execute(host,selected,journal)
    assert r.read(journal/"state.json")["state"]=="FAILED"
    assert "own" not in runtime.containers
    runtime.fail=None
    assert r.execute(host,selected,journal,resume=True)["state"]=="SUCCEEDED"
    assert sum(args[1:]==["rm","-f","own"] for args in runtime.calls)==1
    assert r.execute(host,selected,journal,resume=True)["state"]=="SUCCEEDED"


def test_unknown_inventory_blocks_without_mutation(tmp_path):
    root=tmp_path/"partial";root.mkdir()
    host=r.Host(root,lambda *a,**k:pytest.fail("OS accessed"),tmp_path/"system")
    with pytest.raises(RuntimeError,match="inventory"):r.plan(host)


def test_stopped_partial_install_can_remove_without_core(install):
    host,runtime,journal=install
    runtime.containers["own"]["State"]["Running"]=False
    selected=r.plan(host)
    assert r.execute(host,selected,journal)["state"]=="SUCCEEDED"


def test_update_lease_excludes_removal(install,monkeypatch):
    from core.update_lease import UpdateLease
    host,runtime,journal=install
    monkeypatch.delenv("DATABASE_URL",raising=False)
    monkeypatch.delenv("UPDATE_DATABASE_URL",raising=False)
    selected=r.plan(host)
    with UpdateLease(host.root/"config/update","update"):
        with pytest.raises((OSError,RuntimeError)):r.execute(host,selected,journal)
    assert "own" in runtime.containers


def test_no_docker_daemon_is_not_absence_of_resources(install):
    host,runtime,_=install
    runtime.fail=lambda args:args[:2]==["docker","ps"]
    with pytest.raises(RuntimeError):r.plan(host)


@pytest.mark.parametrize("path",["/","/opt","/etc/vertep","/var/lib/docker/volumes","relative"])
def test_unsafe_roots_are_rejected(path):
    with pytest.raises(RuntimeError):r.safe_root(path)


def test_cli_dispatches_remove_before_sourcing_environment():
    text=(Path(__file__).parents[1]/"scripts/vertep").read_text(encoding="utf-8")
    assert text.index('"${1:-}" == remove') < text.index('source "$ROOT/.env"')
    assert '"$ROOT/scripts/remove-vertep.py" --root "$ROOT" "$@"' in text


def test_bundle_contains_removal_executor():
    root=Path(__file__).parents[1]
    assert '"remove-vertep.py"' in (root/"scripts/build-runtime-bundle.py").read_text()
    assert '"scripts/remove-vertep.py"' in (root/"scripts/release-bundle.py").read_text()


def test_removal_marker_fences_core_even_if_operator_sets_normal(tmp_path,monkeypatch):
    from core import system_state
    monkeypatch.setenv("CONFIG_ROOT",str(tmp_path))
    monkeypatch.setenv("UPDATE_STATE_DIR",str(tmp_path/"update"))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND","file")
    system_state.set_system_state("NORMAL","test")
    (tmp_path/"removal-in-progress.json").write_text("{}",encoding="utf-8")
    assert not system_state.dispatch_allowed()
    assert not system_state.jobs_may_be_created()
    system_state.set_system_state("NORMAL","concurrent resume")
    assert system_state.get_system_state()["operation_id"]=="removal"


def test_foreign_bind_ancestor_of_root_blocks_removal(install):
    host,runtime,_=install
    runtime.containers["foreign"]["Mounts"]=[{"Type":"bind","Source":str(host.root.parent)}]
    assert r.plan(host)["blocked"]


def test_software_symlink_escape_blocks_before_delete(install):
    host,_,_=install
    ledger=r.read(host.root/"config/removal-ownership.json")
    ledger["files"]["../foreign.txt"]="bad"
    r.atomic(host.root/"config/removal-ownership.json",ledger)
    with pytest.raises(RuntimeError,match="inventory"):r.plan(host)


def test_systemd_dropin_is_not_silently_removed(install):
    host,_,_=install
    path=host.unit_path("vertep-core.service").with_name("vertep-core.service.d")
    path.mkdir();(path/"override.conf").write_text("override",encoding="utf-8")
    assert r.plan(host)["blocked"]


def test_dry_run_and_unconfirmed_noninteractive_request_do_not_mutate(install,monkeypatch,capsys):
    host,runtime,_=install
    monkeypatch.setattr(r.sys,"platform","linux")
    monkeypatch.setattr(r.os,"geteuid",lambda:0,raising=False)
    # Permission checks are Linux-only in the production entry point; filesystem proof uses Host directly.
    class Stat:
        st_uid=0
        st_mode=0o700
    real_stat=Path.stat
    def stat(path,*args,**kwargs):
        if path in {host.root,host.root/"config"}:
            value=real_stat(path,*args,**kwargs)
            return SimpleNamespace(**{name:getattr(value,name) for name in dir(value) if name.startswith('st_')} | {"st_uid":0,"st_mode":value.st_mode & ~0o022})
        return real_stat(path,*args,**kwargs)
    monkeypatch.setattr(Path,"stat",stat)
    monkeypatch.setattr(r,"Host",lambda root:host)
    monkeypatch.setattr(r.sys.stdin,"isatty",lambda:False)
    assert r.main(["--root",str(host.root),"--dry-run"])==0
    assert r.main(["--root",str(host.root)])==1
    assert "own" in runtime.containers
    assert not any("rm" in call or "disable" in call for call in runtime.calls)
    assert (host.root/"scripts/vertep").exists()


def test_volume_backing_bind_mount_is_shared(install):
    host,runtime,_=install
    runtime.volumes["data"]["Mountpoint"]="/var/lib/docker/volumes/data/_data"
    runtime.containers["foreign"]["Mounts"]=[{"Type":"bind","Source":"/var/lib/docker/volumes/data/_data"}]
    assert not r.plan(host,True)["volumes"]


def _as_linux_root(monkeypatch, host):
    monkeypatch.setattr(r.sys, "platform", "linux")
    monkeypatch.setattr(r.os, "geteuid", lambda: 0, raising=False)
    real_stat = Path.stat
    def stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if path in {host.root, host.root/"config"}:
            return SimpleNamespace(**{name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
                                   | {"st_uid": 0, "st_mode": value.st_mode & ~0o022})
        return value
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(r, "Host", lambda root: host)
    monkeypatch.setattr(r.sys.stdin, "isatty", lambda: False)


def test_image_present_before_installation_is_retained(install):
    host, runtime, journal = install
    ledger_path = host.root/"config/removal-ownership.json"
    ledger = json.loads(ledger_path.read_text())
    assert ledger["image_provenance"]["preexisting"] == ["sha256:foreign"]
    ledger["image_provenance"]["preexisting"].append("sha256:own")  # e.g. postgres pulled by the operator earlier
    ledger_path.write_text(json.dumps(ledger))
    selected = r.plan(host)
    assert selected["images"] == []
    assert "Образ існував до інсталяції Vertep: sha256:own" in selected["retained"]
    assert r.execute(host, selected, journal)["state"] == "SUCCEEDED"
    assert "sha256:own" in runtime.images


def test_legacy_ledger_removes_only_images_of_the_signed_lock(install):
    host, runtime, journal = install
    ledger_path = host.root/"config/removal-ownership.json"
    ledger = json.loads(ledger_path.read_text())
    ledger.pop("image_provenance")  # inventory created after Vertep was already running
    ledger_path.write_text(json.dumps(ledger))
    third = copy.deepcopy(runtime.containers["own"])
    third.update({"Id": "pg", "Image": "sha256:pg"})
    third["Config"]["Labels"]["com.docker.compose.service"] = "postgres"
    runtime.containers["pg"] = third
    runtime.networks["private"]["Containers"]["pg"] = {}
    runtime.images.add("sha256:pg")
    runtime.digests = {"sha256:own": ["ghcr.io/owner/vertep-core@sha256:aa"],
                       "sha256:pg": ["postgres@sha256:bb"]}
    (host.root/"images.json").write_text(json.dumps(
        {"core": {"reference": "ghcr.io/owner/vertep-core:0.0.3.64", "digest": "sha256:aa", "platforms": []}}))
    selected = r.plan(host)
    assert selected["images"] == ["sha256:own"]
    assert "Походження образу не зафіксоване: sha256:pg" in selected["retained"]
    r.execute(host, selected, journal)
    assert runtime.images == {"sha256:foreign", "sha256:pg"}


def test_purge_removes_emptied_layout_including_root(install):
    host, runtime, journal = install
    result = r.execute(host, r.plan(host, True), journal)
    assert result["state"] == "SUCCEEDED"
    assert not host.root.exists()
    assert json.loads((journal/"state.json").read_text())["state"] == "SUCCEEDED"


def test_purge_keeps_unknown_content_and_reports_partial(install):
    host, runtime, journal = install
    selected = r.plan(host, True)
    (host.root/"operator-notes.txt").write_text("not Vertep")
    result = r.execute(host, selected, journal)
    assert result["state"] == "PARTIAL"
    assert (host.root/"operator-notes.txt").read_text() == "not Vertep"
    assert str(host.root/"operator-notes.txt") in result["retained_unknown"]
    assert not (host.root/"config").exists() and not (host.root/"storage").exists()


def test_cli_resume_finds_the_unfinished_journal(install, monkeypatch, tmp_path):
    host, runtime, _ = install
    base = tmp_path/"journals"
    monkeypatch.setattr(r, "JOURNAL_BASE", base)
    selected = r.plan(host)
    runtime.fail = lambda args: args[:3] == ["docker", "image", "rm"]
    with pytest.raises(RuntimeError):
        r.execute(host, selected, base/"key-plan")
    runtime.fail = None
    # Another installation and a finished run are never resumed.
    (base/"other").mkdir()
    (base/"other/state.json").write_text(json.dumps({"state": "FAILED", "plan": {"root": "/srv/other"}}))
    (base/"done").mkdir()
    (base/"done/state.json").write_text(json.dumps({"state": "SUCCEEDED", "plan": {"root": str(host.root)}}))
    _as_linux_root(monkeypatch, host)
    assert r.main(["--root", str(host.root), "--resume", "--confirm", selected["plan_sha256"]]) == 0
    assert json.loads((base/"key-plan/state.json").read_text())["state"] == "SUCCEEDED"
    assert "sha256:own" not in runtime.images


def test_resume_without_or_with_ambiguous_journal_is_refused(tmp_path):
    base = tmp_path/"journals"
    with pytest.raises(RuntimeError, match="Немає незавершеного"):
        r.find_journal(base, Path("/srv/vertep"))
    for name in ["a", "b"]:
        (base/name).mkdir(parents=True)
        (base/name/"state.json").write_text(json.dumps({"state": "FAILED", "plan": {"root": "/srv/vertep"}}))
    with pytest.raises(RuntimeError, match="Кілька"):
        r.find_journal(base, Path("/srv/vertep"))


def test_reinstall_on_retained_data_keeps_identity_and_provenance(install):
    host, runtime, journal = install
    before = json.loads((host.root/"config/removal-ownership.json").read_text())
    own = copy.deepcopy(runtime.containers["own"])
    assert r.execute(host, r.plan(host), journal)["state"] == "SUCCEEDED"
    # Bootstrap on the same root: retained config/data, new runtime files, units and containers.
    (host.root/"scripts").mkdir(exist_ok=True)
    (host.root/"scripts/vertep").write_text("runtime v2")
    (host.root/"docker-compose.yml").write_text("services: {}")
    (host.root/"vertep-core.service").write_text("ExecStart=@VERTEP_ROOT@/scripts/vertep")
    (host.root/"manifest.json").write_text(json.dumps({"files": {"scripts/vertep": {}, "docker-compose.yml": {}, "vertep-core.service": {}}}))
    (host.system/"etc/systemd/system/vertep-core.service").write_text("ExecStart="+str(host.root/"scripts/vertep"))
    r.record(host)
    runtime.containers["own"] = own
    runtime.images.add("sha256:own")
    runtime.networks["private"] = {"Id": "net-own", "Labels": {"com.docker.compose.network": "default"}, "Containers": {"own": {}}}
    after = r.record(host)
    assert after["installation_id"] == before["installation_id"]
    assert after["image_provenance"] == before["image_provenance"]
    assert set(after["files"]) <= {name for name in after["files"] if (host.root/name).exists()}
    selected = r.plan(host)
    assert selected["blocked"] == [] and selected["images"] == ["sha256:own"]


def test_bootstrap_refuses_reinstall_over_unfinished_removal(tmp_path):
    import shutil, subprocess
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash unavailable")
    source = (Path(__file__).resolve().parents[1]/"bootstrap.sh").read_text(encoding="utf-8")
    guard = next(line for line in source.splitlines() if "removal-in-progress.json" in line)
    block = source[source.index(guard):].split("\n\n", 1)[0]
    script = 'set -eu\nfail(){ echo "REFUSED: $*"; exit 17; }\n' + block + '\necho PASSED\n'
    (tmp_path/"config").mkdir()
    run = lambda: subprocess.run([bash, "-c", script], env={"INSTALL_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"},
                                 capture_output=True, text=True)
    assert run().stdout.strip() == "PASSED"
    (tmp_path/"config/removal-in-progress.json").write_text("{}")
    result = run()
    assert result.returncode == 17 and "removal is unfinished" in result.stdout
