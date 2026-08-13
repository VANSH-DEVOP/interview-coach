# Convenience targets. Everything here is a plain docker compose command with
# one addition: GIT_SHA, which compose stamps into SENTRY_RELEASE so an error
# report can answer "since when".
#
# `docker compose up` still works on its own. The release is simply unset, and
# an unset release is the state this project was in until now -- nothing breaks,
# you just cannot tell which build an error came from.

GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null)
export GIT_SHA

.PHONY: up down restart logs ps build test

## Build and start the stack, stamped with the current commit.
up:
	docker compose up -d --build

## Start without rebuilding. The release still reflects the working tree, which
## is only misleading if HEAD has moved since the image was built.
restart:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

## Everything CI runs, against the local stack.
test:
	cd backend && pytest -q --cov=app
	cd frontend && npm run typecheck && npm run test:coverage
