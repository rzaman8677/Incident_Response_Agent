.PHONY: install doctor llm-check test eval demo demo-deterministic serve

install:
	python -m pip install -e '.[all]'

doctor:
	sentinelops doctor

llm-check:
	sentinelops llm-check

test:
	python -m pytest

eval:
	sentinelops eval

demo:
	sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve

demo-deterministic:
	sentinelops demo --fault bad_deployment --service checkout --mode assisted --approve --backend deterministic

serve:
	sentinelops serve
