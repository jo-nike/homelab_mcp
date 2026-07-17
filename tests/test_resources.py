"""Tests for MCP resource handlers: docs, stacks, vault notes."""

import pytest

import config
import resources


@pytest.fixture
def populated_data_dirs(tmp_path, monkeypatch):
    """Set up test fixtures with sample files in temp directories."""
    # Create sample docs
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "monitoring.md").write_text("# Monitoring\n\nPrometheus and Loki setup.")
    (docs / "storage.md").write_text("# Storage\n\nNAS and PBS configuration.")

    # Create sample stacks
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    stack_dir = stacks / "dh_grafana_stack"
    stack_dir.mkdir()
    (stack_dir / "docker-compose.yaml").write_text(
        "version: '3'\nservices:\n  grafana:\n    image: grafana/grafana"
    )

    # Create sample vault
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Services.md").write_text("# Services\n\nAll homelab services overview.")

    monkeypatch.setattr(config, "DOCS_DIR", docs)
    monkeypatch.setattr(config, "STACKS_DIR", stacks)
    monkeypatch.setattr(config, "VAULT_DIR", vault)
    return tmp_path


@pytest.fixture
def registered_mcp(populated_data_dirs):
    """Register resources on a fresh FastMCP instance."""
    from fastmcp import FastMCP

    mcp = FastMCP("test")
    resources.register(mcp)
    return mcp


@pytest.fixture
def secret_outside_bases(populated_data_dirs):
    """Plant a secret file above the resource base dirs to attempt to exfil."""
    secret = populated_data_dirs / "secret.md"
    secret.write_text("SECRET_TOKEN_XYZ")
    return populated_data_dirs


def _get_content(result) -> str:
    """Extract text content from a ResourceResult."""
    return result.contents[0].content


@pytest.mark.asyncio
async def test_get_doc_exists(registered_mcp):
    """homelab://docs/monitoring returns content of monitoring.md."""
    result = await registered_mcp.read_resource("homelab://docs/monitoring")
    content = _get_content(result)
    assert "Prometheus" in content


@pytest.mark.asyncio
async def test_get_doc_not_found(registered_mcp):
    """homelab://docs/nonexistent returns 'not found' message."""
    result = await registered_mcp.read_resource("homelab://docs/nonexistent")
    content = _get_content(result)
    assert "not found" in content.lower() or "Not found" in content


@pytest.mark.asyncio
async def test_get_doc_without_extension(registered_mcp):
    """homelab://docs/monitoring.md also works."""
    result = await registered_mcp.read_resource("homelab://docs/monitoring.md")
    content = _get_content(result)
    assert "Prometheus" in content


@pytest.mark.asyncio
async def test_get_stack_exists(registered_mcp):
    """homelab://stacks/dh_grafana_stack returns docker-compose content."""
    result = await registered_mcp.read_resource("homelab://stacks/dh_grafana_stack")
    content = _get_content(result)
    assert "grafana" in content


@pytest.mark.asyncio
async def test_get_stack_not_found(registered_mcp):
    """homelab://stacks/nonexistent returns 'not found' message."""
    result = await registered_mcp.read_resource("homelab://stacks/nonexistent")
    content = _get_content(result)
    assert "not found" in content.lower() or "Not found" in content


@pytest.mark.asyncio
async def test_get_vault_note_exists(registered_mcp):
    """homelab://vault/Services.md returns vault note content."""
    result = await registered_mcp.read_resource("homelab://vault/Services.md")
    content = _get_content(result)
    assert "homelab services" in content.lower()


@pytest.mark.asyncio
async def test_get_vault_note_not_found(registered_mcp):
    """homelab://vault/nonexistent returns 'not found' message."""
    result = await registered_mcp.read_resource("homelab://vault/nonexistent")
    content = _get_content(result)
    assert "not found" in content.lower() or "Not found" in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "homelab://docs/..%2Fsecret",
        "homelab://docs/..%2Fsecret.md",
        "homelab://stacks/..%2Fsecret.md",
        "homelab://vault/..%2Fsecret",
        "homelab://vault/..%2Fsecret.md",
    ],
)
async def test_path_traversal_is_blocked(secret_outside_bases, registered_mcp, uri):
    """Encoded ../ URIs must not escape the base dir to read outside files."""
    result = await registered_mcp.read_resource(uri)
    content = _get_content(result)
    assert "SECRET_TOKEN_XYZ" not in content
    assert "not found" in content.lower()


@pytest.mark.asyncio
async def test_doc_index_shadows_disk(registered_mcp, monkeypatch):
    """The registry index is the production path (Gitea refresh populates it) and
    wins over the on-disk file when both exist."""
    monkeypatch.setattr(
        config, "DOCS_INDEX", {"monitoring.md": {"content": "INDEX CONTENT WINS"}}
    )
    result = await registered_mcp.read_resource("homelab://docs/monitoring")
    content = _get_content(result)
    assert content == "INDEX CONTENT WINS"
    assert "Prometheus" not in content  # the disk file was not read


@pytest.mark.asyncio
async def test_stack_index_shadows_disk(registered_mcp, monkeypatch):
    """STACKS_INDEX content wins over the on-disk compose file."""
    monkeypatch.setattr(
        config, "STACKS_INDEX", {"dh_grafana_stack": "INDEX STACK BODY"}
    )
    result = await registered_mcp.read_resource("homelab://stacks/dh_grafana_stack")
    assert _get_content(result) == "INDEX STACK BODY"


@pytest.mark.asyncio
async def test_vault_index_shadows_disk(registered_mcp, monkeypatch):
    """VAULT_INDEX content wins over the on-disk note."""
    monkeypatch.setattr(
        config, "VAULT_INDEX", {"Services.md": {"content": "INDEX VAULT BODY"}}
    )
    result = await registered_mcp.read_resource("homelab://vault/Services.md")
    assert _get_content(result) == "INDEX VAULT BODY"


@pytest.mark.asyncio
async def test_not_found_lists_index_and_disk_names(registered_mcp, monkeypatch):
    """The 'Available:' miss listing merges index-only keys with on-disk files."""
    monkeypatch.setattr(
        config, "DOCS_INDEX", {"index_only.md": {"content": "only in index"}}
    )
    result = await registered_mcp.read_resource("homelab://docs/nope")
    content = _get_content(result)
    assert "not found" in content.lower()
    assert "index_only.md" in content  # index-only name
    assert "monitoring" in content  # on-disk name


@pytest.mark.asyncio
async def test_list_docs(registered_mcp):
    """Resource templates list available docs."""
    templates = await registered_mcp.list_resource_templates()
    template_uris = [t.uri_template for t in templates]
    assert any("docs" in uri for uri in template_uris)


@pytest.mark.asyncio
async def test_list_stacks(registered_mcp):
    """Resource templates list available stacks."""
    templates = await registered_mcp.list_resource_templates()
    template_uris = [t.uri_template for t in templates]
    assert any("stacks" in uri for uri in template_uris)
