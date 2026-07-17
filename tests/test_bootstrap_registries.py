"""Tests for scripts/bootstrap_registries.py -- registry skeleton generation."""

import pytest
import yaml

import config
from scripts.bootstrap_registries import (
    RENDERERS,
    build_baselines,
    build_hosts,
    build_services,
    build_topology,
    main,
    render_all,
    write_files,
)

# Canned live payload in the shapes _fetch_all_sources produces
# (see tests/test_refresh.py for the upstream fixture shapes).
LIVE = {
    "proxmox_vms": [
        # Real cluster/resources node entries have no `name`, only `node`.
        {"name": "", "type": "node", "status": "online", "node": "pve"},
        {
            "name": "docker-host",
            "type": "vm",
            "status": "running",
            "node": "pve",
            "vmid": 100,
            "cpu_cores": 4,
            "ram_total_bytes": 8 * 1024**3,
        },
        {
            "name": "media-box",
            "type": "ct",
            "status": "running",
            "node": "pve",
            "vmid": 101,
        },
    ],
    "portainer_containers": [
        {
            # ip deliberately empty: the fetcher's endpoint-IP resolution needs
            # the runtime HOSTS registry, so bootstrap must backfill from its
            # own discovered hosts.
            "name": "prometheus",
            "host": "docker-host",
            "ip": "",
            "status": "running",
            "image": "prom/prometheus:latest",
            "ports": [9090],
            "mcp_role": "metrics collection",
            "mcp_auth": "none",
        },
        {
            "name": "grafana",
            "host": "docker-host",
            "ip": "192.168.1.79",
            "status": "running",
            "image": "grafana/grafana:latest",
            "ports": [3000],
            "mcp_role": "",
            "mcp_auth": "",
        },
        {
            "name": "old-thing",
            "host": "docker-host",
            "ip": "192.168.1.79",
            "status": "exited",
            "image": "old:1",
            "ports": [],
            "mcp_role": "",
            "mcp_auth": "",
        },
    ],
    "scanopy_hosts": [
        {"ip": "192.168.1.79", "hostname": "Docker Host", "mac": "AA:BB"},
        {"ip": "192.168.1.50", "hostname": "nas", "mac": "CC:DD"},
    ],
    "npm_hosts": [
        {
            "domain": "grafana.example.com",
            "forward_host": "192.168.1.79",
            "forward_port": 3000,
            "ssl": True,
        }
    ],
    "prometheus_instances": ["docker-host", "totally-unmatchable-gpu"],
    "dns_records": [],
    "wireguard_peers": [],
    "healthchecks": [],
    "gitea_repos": [],
}

EMPTY_LIVE = {k: [] for k in LIVE}


def test_build_hosts_discovers_and_prefills_aliases():
    hosts, unmatched = build_hosts(LIVE)
    by_name = {h["name"]: h for h in hosts}

    assert by_name["pve"]["proxmox_node"] == "pve"
    dh = by_name["docker-host"]
    assert dh["parent"] == "pve"
    assert dh["specs"] == {"cpu": "4 vCPU", "ram": "8 GB"}
    # Scanopy "Docker Host" normalizes onto docker-host and fills the IP.
    assert dh["ip"] == "192.168.1.79"
    assert dh["aliases"]["portainer"] == "docker-host"
    assert dh["aliases"]["prometheus"] == ["docker-host"]
    # Scanopy-only host still appears.
    assert by_name["nas"]["ip"] == "192.168.1.50"
    # Unmatchable upstream id is reported, never guessed onto a host.
    assert unmatched["prometheus"] == ["totally-unmatchable-gpu"]
    for host in hosts:
        assert host["role"].startswith("TODO") or host["role"]


def test_build_services_labels_and_domains():
    hosts, _ = build_hosts(LIVE)
    services = build_services(LIVE, hosts)
    by_name = {s["name"]: s for s in services}

    prom = by_name["prometheus"]
    assert prom["role"] == "metrics collection"  # from mcp.role label
    assert prom["auth"] == "none"
    assert prom["port"] == 9090
    # Empty container ip backfilled from the discovered docker-host entry.
    assert prom["ip"] == "192.168.1.79"
    graf = by_name["grafana"]
    assert graf["role"].startswith("TODO")  # no label -> stub
    assert graf["domain"] == "grafana.example.com"  # matched from NPM
    assert prom["domain"] is None


def test_build_baselines_counts_running_only():
    baselines = build_baselines(build_services(LIVE), LIVE)
    assert baselines["docker-host"]["expected_container_count"] == 2
    assert baselines["docker-host"]["expected_services"] == ["grafana", "prometheus"]


def test_build_topology_parent_tree():
    hosts, _ = build_hosts(LIVE)
    topology = build_topology(hosts, build_services(LIVE))
    stacks = {s["host"]: s for s in topology["vertical_stacks"]}
    assert "pve" in stacks
    child_names = [c["name"] for c in stacks["pve"]["children"]]
    assert "docker-host" in child_names
    dh = next(c for c in stacks["pve"]["children"] if c["name"] == "docker-host")
    assert "prometheus" in dh["services"]
    # Human-only sections are stubs, present so load_topology round-trips.
    assert topology["critical_containers"] == []
    assert topology["dependencies"] == []


def test_rendered_yaml_round_trips_through_loaders(tmp_path, monkeypatch):
    rendered = render_all(LIVE)
    assert set(rendered) == {f"{n}.yaml" for n in RENDERERS}
    for filename, text in rendered.items():
        (tmp_path / filename).write_text(text)

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    hosts = config.load_hosts()
    assert "docker-host" in hosts
    assert hosts["docker-host"]["aliases"]["portainer"] == "docker-host"
    services = config.load_services()
    assert services["prometheus"]["port"] == 9090
    baselines = config.load_baselines()
    assert baselines["baselines"]["docker-host"]["expected_container_count"] == 2
    assert "cpu_percent" in baselines["metric_queries"]
    topology = config.load_topology()
    assert topology["vertical_stacks"]
    # The unmatched-alias TODO block must be comments, invisible to YAML.
    assert "totally-unmatchable-gpu" in rendered["hosts.yaml"]
    parsed = yaml.safe_load(rendered["hosts.yaml"])
    assert "totally-unmatchable-gpu" not in str(parsed)


def test_empty_live_renders_valid_templates(tmp_path, monkeypatch):
    rendered = render_all(EMPTY_LIVE)
    for filename, text in rendered.items():
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict), filename
        (tmp_path / filename).write_text(text)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert config.load_hosts() == {}
    assert config.load_services() == {}


def test_write_files_refuses_overwrite_without_force(tmp_path):
    (tmp_path / "hosts.yaml").write_text("hosts: []\n")
    rendered = render_all(EMPTY_LIVE, only=["hosts"])
    with pytest.raises(SystemExit, match="hosts.yaml"):
        write_files(tmp_path, rendered, force=False)
    assert (tmp_path / "hosts.yaml").read_text() == "hosts: []\n"

    write_files(tmp_path, rendered, force=True)
    assert "bootstrap_registries" in (tmp_path / "hosts.yaml").read_text()


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    async def fake_gather():
        return dict(LIVE), ["portainer", "proxmox"]

    monkeypatch.setattr("scripts.bootstrap_registries.gather_live", fake_gather)
    rc = main(["--dry-run", "--out", str(tmp_path)])
    assert rc == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr()
    assert "hosts.yaml" in out.out
    assert "configured sources: portainer, proxmox" in out.err


def test_main_only_filter(tmp_path, monkeypatch):
    async def fake_gather():
        return dict(LIVE), ["proxmox"]

    monkeypatch.setattr("scripts.bootstrap_registries.gather_live", fake_gather)
    rc = main(["--out", str(tmp_path), "--only", "hosts", "--only", "services"])
    assert rc == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "hosts.yaml",
        "services.yaml",
    ]
