.PHONY: dev http sync-data test lint typecheck format hooks publish-mirror bootstrap

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
