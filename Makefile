.PHONY: demo up speel integratie test lint check down opname

COMPOSE := docker compose --progress quiet

.env:
	cp .env.example .env

## demo: start de keten schoon en speel de scenario's af (één commando)
demo: up speel

## up: start Keycloak, inrichting en dossier-API vanuit een schone toestand
up: .env
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --wait

## speel: speel de scenario's af tegen de draaiende keten
speel:
	@$(COMPOSE) run --rm demo

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
	timeout 600 ffmpeg -nostdin -y -loglevel error -i docs/beeld/demo.mp4 \
		-vf "fps=6,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=24:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
		docs/beeld/demo.gif
