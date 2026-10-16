.PHONY: demo integratie test lint check down opname

COMPOSE := docker compose

.env:
	cp .env.example .env

## demo: start de keten schoon en speel de drie scenario's af (één commando)
demo: .env
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) run --rm demo

## integratie: alle integratietests tegen de echte keten
integratie: .env
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) run --rm tests

## test: unittests op de host
test:
	uv run pytest -m "not integratie"

## lint: ruff, opmaak en typecontrole
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check src tests

check: lint test integratie

down:
	$(COMPOSE) down -v --remove-orphans

## opname: neem de demo op met VHS (demo/demo.tape)
opname: .env
	vhs demo/demo.tape
