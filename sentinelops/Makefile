.PHONY: install test eval demo serve

install:
	python -m pip install -e '.[all]'

test:
	python -m pytest

eval:
	sentinelops eval

demo:
	sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve

serve:
	sentinelops serve
