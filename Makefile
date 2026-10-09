FIXTURE_PATH ?= tests/fixtures/lifecycle.json
RUN = docker compose run --rm --build

up:
	docker compose up -d --wait postgres

crawl: up
	$(RUN) crawler python -m crawler.run

test: up
	$(RUN) -v "$(abspath $(FIXTURE_PATH)):/fixture.json:ro" -e FIXTURE_PATH=/fixture.json crawler python -m crawler.testing
