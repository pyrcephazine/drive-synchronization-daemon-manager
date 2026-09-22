PYTHON := /usr/bin/python3

.PHONY: test run demo deb install-user
test:
	PYTHONPATH=. $(PYTHON) tests/run_tests.py
run:
	$(PYTHON) bin/drive-synchronization-daemon-manager
demo:
	$(PYTHON) bin/drive-synchronization-daemon-manager --demo
deb:
	$(PYTHON) scripts/package.py build-deb
install-user:
	$(PYTHON) scripts/package.py install-user
