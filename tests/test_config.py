"""Tests for config module: YAML loading, index building, instructions generation."""

import config


def test_load_services(sample_services_yaml, monkeypatch):
    """load_services() returns dict keyed by service name."""
    monkeypatch.setattr(config, "DATA_DIR", sample_services_yaml.parent)
    result = config.load_services()
    assert isinstance(result, dict)
    assert "prometheus" in result
    assert result["prometheus"]["port"] == 9090
    assert "plex" in result
    assert result["plex"]["host"] == "plex-stack"


def test_load_hosts(sample_hosts_yaml, monkeypatch):
    """load_hosts() returns dict keyed by hostname."""
    monkeypatch.setattr(config, "DATA_DIR", sample_hosts_yaml.parent)
    result = config.load_hosts()
    assert isinstance(result, dict)
    assert "docker-host" in result
    assert result["docker-host"]["ip"] == "192.168.1.79"
    assert "plex-stack" in result
    assert result["plex-stack"]["role"] == "media server stack"


def test_load_services_skips_nameless_entry(tmp_path, monkeypatch):
    """A services.yaml entry without 'name' is skipped, not a startup crash."""
    (tmp_path / "services.yaml").write_text(
        "services:\n"
        "  - name: prometheus\n"
        "    port: 9090\n"
        "  - port: 3000\n"  # no name -- must be skipped, not KeyError
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    result = config.load_services()
    assert "prometheus" in result
    assert len(result) == 1


def test_load_hosts_skips_nameless_entry(tmp_path, monkeypatch):
    """A hosts.yaml entry without 'name' is skipped, not a startup crash."""
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n"
        "  - name: beast\n"
        '    ip: "192.168.1.119"\n'
        '  - ip: "192.168.1.200"\n'  # no name -- must be skipped
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    result = config.load_hosts()
    assert "beast" in result
    assert len(result) == 1


def test_build_ip_index(
    sample_services_yaml, sample_hosts_yaml, monkeypatch, restore_config_registries
):
    """build_ip_index() cross-references hosts and services by IP."""
    # Both sample fixtures already write into the same tmp_path, so services.yaml
    # and hosts.yaml sit side by side -- no copy needed.
    monkeypatch.setattr(config, "DATA_DIR", sample_services_yaml.parent)

    # Populate mutable registries (restored by the fixture on teardown).
    config.SERVICES.clear()
    config.HOSTS.clear()
    config.SERVICES.update(config.load_services())
    config.HOSTS.update(config.load_hosts())

    result = config.build_ip_index()
    assert "192.168.1.79" in result
    assert result["192.168.1.79"]["host"]["name"] == "docker-host"
    service_names = [s["name"] for s in result["192.168.1.79"]["services"]]
    assert "prometheus" in service_names


def test_load_docs_index(sample_docs_dir, monkeypatch):
    """load_docs_index() indexes markdown files with sections."""
    monkeypatch.setattr(config, "DOCS_DIR", sample_docs_dir)
    result = config.load_docs_index()
    assert "test.md" in result
    assert len(result["test.md"]["sections"]) >= 2
    assert "monitoring" in result["test.md"]["content"].lower()


def test_build_instructions(sample_services_yaml, sample_hosts_yaml, monkeypatch):
    """build_instructions() renders the topology table from the loaded data.

    Point DATA_DIR at the sample fixtures and assert on data-derived content (a
    known host row and its service), not just static prose literals."""
    monkeypatch.setattr(config, "DATA_DIR", sample_services_yaml.parent)
    result = config.build_instructions()
    assert isinstance(result, str)
    # docker-host lives at 192.168.1.79 -> rendered as the ".79" shorthand, with
    # its prometheus service in the Key Services column.
    assert "docker-host" in result
    assert ".79" in result
    assert "prometheus" in result
    # And the tool catalog sections are present.
    assert "tool categories" in result.lower()
    assert "write tools" in result.lower()
