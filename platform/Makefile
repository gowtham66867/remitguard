.PHONY: test lint eval quality

test:
	python -m pytest tests/ -v

lint:
	python -m ruff check .

eval:
	python eval_semantic.py --no-concurrency

quality: lint test eval
