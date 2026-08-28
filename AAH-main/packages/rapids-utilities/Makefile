.PHONY: rust test bench lint all install

install:
	pip install -e ".[dev]"

rust:
	cd codemap_rs && maturin develop --release

test:
	pytest tests/ -v

test-fast:
	pytest tests/ -v -m "not slow"

bench:
	python -m codemap_scale.benchmark

lint:
	ruff check .

all: lint test
