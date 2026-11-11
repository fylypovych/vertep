"""Disposable Docker resources; systemd is simulated, no host installation is removed."""
import json
from pathlib import Path
import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from core import removal as r


def docker(*args, check=True):
    return subprocess.run(["docker",*args],capture_output=True,text=True,encoding="utf-8",check=check,timeout=180)


@pytest.fixture
def disposable(tmp_path):
    if not shutil.which("docker") or docker("info",check=False).returncode:
        pytest.skip("Docker daemon unavailable; not real-server acceptance")
    image="alpine:3.20"
    if docker("image","inspect",image,check=False).returncode:
        docker("pull",image)
    name="vertep-remove-"+uuid.uuid4().hex[:12]
    root=tmp_path/"installation"; (root/"scripts").mkdir(parents=True)
    (root/"scripts/vertep").write_text("runtime",encoding="utf-8")
    (root/"config").mkdir()
    (root/"config/secret").write_text("retained",encoding="utf-8")
    (root/"manifest.json").write_text(json.dumps({"files":{"scripts/vertep":{}}}),encoding="utf-8")
    (root/"docker-compose.yml").write_text("services: {}",encoding="utf-8")
    docker("volume","create","--label","com.docker.compose.volume=data",name)
    network=docker("network","create","--label","com.docker.compose.network=default",name).stdout.strip()
    ids=[]
    try:
        own=docker("create","--name",name,"--network",name,"--mount",f"type=volume,source={name},target=/data",
                   "--label",f"vertep.removal.test={name}",
                   "--label",f"com.docker.compose.project.working_dir={root}",
                   "--label",f"com.docker.compose.project.config_files={root/'docker-compose.yml'}",
                   "--label","com.docker.compose.service=core",image,"sleep","300").stdout.strip()
        ids.append(own)
        foreign=docker("run","-d","--name",name+"-foreign","--label",f"vertep.removal.test={name}",image,"sleep","300").stdout.strip()
        ids.append(foreign)
        def runner(args,**kwargs):
            if args[0]=="systemctl":return SimpleNamespace(returncode=0,stdout="inactive",stderr="")
            # Other xdist workers own independent Docker fixtures. Observe our
            # disposable host namespace while retaining real inspect/remove.
            if args == ["docker", "ps", "-aq"]:
                args = [*args, "--filter", f"label=vertep.removal.test={name}"]
            return subprocess.run(args,**{**kwargs,"encoding":"utf-8"})
        host=r.Host(root,runner,tmp_path/"system")
        r.record(host)
        yield host,own,foreign,name,tmp_path/"journal"
    finally:
        for ident in ids:docker("rm","-f",ident,check=False)
        docker("network","rm",network,check=False)
        docker("volume","rm",name,check=False)


@pytest.mark.parametrize("purge",[False,True])
def test_real_docker_removes_only_disposable_installation(disposable,purge):
    host,own,foreign,volume,journal=disposable
    selected=r.plan(host,purge)
    assert selected["containers"]==[own]
    # The base image is also used by a foreign container and must be retained.
    assert not selected["images"]
    state=r.execute(host,selected,journal)
    assert state["state"]=="SUCCEEDED"
    assert docker("inspect",own,check=False).returncode != 0
    assert json.loads(docker("inspect",foreign).stdout)[0]["State"]["Running"]
    assert (docker("volume","inspect",volume,check=False).returncode != 0) is purge
    assert (host.root/"config/secret").exists() is (not purge)
