.PHONY: dev http sync-data test lint typecheck format hooks publish-mirror bootstrap scan

dev:
	uv run python server.py

http:
	MCP_TRANSPORT=streamable-http uv run python server.py

sync-data:
	rsync -av --delete /home/user/Code/homelab/docs/ data/docs/
	rsync -av --delete /home/user/Code/homelab/docker-stacks/ data/stacks/
	@echo "Syncing vault notes (files > 100 bytes only)..."
	@mkdir -p data/vault
	@find "/home/user/Obsidian Vault/Infrastructures/Homelab/" -name "*.md" -size +100c -exec cp {} data/vault/ \;
	@echo "Data sync complete"

test:
	uv run pytest tests/ -x -v

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run ty check .

format:
	uv run ruff format .
	uv run ruff check --fix .

hooks:
	git config core.hooksPath .githooks

publish-mirror:
	uv run scripts/publish_mirror.py $(if $(REMOTE),--remote $(REMOTE),)

bootstrap:
	uv run scripts/bootstrap_registries.py $(ARGS)

# Audit every tool's description for prompt-injection / tool-poisoning with
# Snyk Agent Scan (the maintained successor to Invariant's mcp-scan). The scan
# config launches the server over stdio with dummy credentials so all tool
# modules pass their "is it set?" registration guards and the full tool surface
# is exposed; scanning only reads descriptions, never invokes tools, so no real
# upstream is ever contacted. --dangerously-run-mcp-servers auto-consents to
# launching our own trusted server (skips the interactive y/N prompt).
# Needs a Snyk token (free account: app.snyk.io/account): prefers an already
# exported $SNYK_TOKEN (e.g. CI), else pulls just the SNYK_TOKEN line from the
# gitignored .env. Errors clearly if neither is set.
scan:
	@TOKEN="$${SNYK_TOKEN:-$$(grep -E '^SNYK_TOKEN=' .env 2>/dev/null | tail -1 | sed -e 's/^SNYK_TOKEN=//' -e 's/^"//' -e 's/"$$//' -e 's/\r$$//')}"; \
	if [ -z "$$TOKEN" ]; then echo "make scan: SNYK_TOKEN not set — export it or add 'SNYK_TOKEN=...' to .env"; exit 1; fi; \
	SNYK_TOKEN="$$TOKEN" uvx snyk-agent-scan@latest scan/mcp-scan-config.json --dangerously-run-mcp-servers
