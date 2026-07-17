"""Tests for canonical host-name resolution."""

import pytest

from lib import hosts

pytestmark = pytest.mark.usefixtures("canonical_hosts")


# --- resolve_host ---


def test_resolves_alias_within_its_kind():
    """Each upstream's own id maps back to the canonical name."""
    assert hosts.resolve_host("ai-vm-gpu", "prometheus") == "ai-vm"
    assert hosts.resolve_host("AI", "proxmox") == "ai-vm"
    assert hosts.resolve_host("docker host", "portainer") == "docker-host"
    assert hosts.resolve_host("pve", "proxmox") == "proxmox"


def test_resolves_canonical_name_without_an_alias():
    """An upstream that already uses the canonical name needs no alias entry."""
    assert hosts.resolve_host("beast", "prometheus") == "beast"
    assert hosts.resolve_host("plex-stack", "proxmox") == "plex-stack"


def test_matching_is_case_insensitive():
    """Portainer's "Beast" is the same iron as `beast`."""
    assert hosts.resolve_host("Beast", "portainer") == "beast"
    assert hosts.resolve_host("BEAST", "prometheus") == "beast"


def test_alias_does_not_leak_across_kinds():
    """`AI` is a Proxmox/Portainer name; Prometheus has no such instance."""
    assert hosts.resolve_host("AI", "prometheus") is None
    assert hosts.resolve_host("ai-vm-gpu", "portainer") is None


def test_resolves_across_every_kind_when_kind_is_omitted():
    assert hosts.resolve_host("ai-vm-gpu") == "ai-vm"
    assert hosts.resolve_host("docker host") == "docker-host"
    assert hosts.resolve_host("pve") == "proxmox"


def test_unknown_and_empty_ids_resolve_to_none():
    """A guest that isn't one of our hosts (a template) has no canonical name —
    None is the answer, not a guess."""
    assert hosts.resolve_host("temp-debian-12", "proxmox") is None
    assert hosts.resolve_host("srv", "portainer") is None


# --- canonical_prometheus_host ---


def test_canonical_prometheus_host_strips_port():
    """Prometheus instance labels carry a :port suffix the alias table lacks."""
    assert hosts.canonical_prometheus_host("ai-vm-gpu:9835") == "ai-vm"
    assert hosts.canonical_prometheus_host("beast:9100") == "beast"


def test_canonical_prometheus_host_unknown_is_none():
    assert hosts.canonical_prometheus_host("10.0.0.5:9100") is None
    assert hosts.canonical_prometheus_host("") is None
    assert hosts.resolve_host("") is None


# --- host_parent ---


def test_parent_of_a_guest_is_its_hypervisor():
    assert hosts.host_parent("ai-vm") == "proxmox"
    assert hosts.host_parent("docker-host") == "proxmox"


def test_host_with_no_parent_is_the_iron():
    assert hosts.host_parent("beast") is None
    assert hosts.host_parent("proxmox") is None


def test_parent_of_an_unknown_host_is_none():
    assert hosts.host_parent("nope") is None
    assert hosts.host_parent("") is None
