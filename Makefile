# Convenience wrappers around the venv.
#
# Every target clears PYTHONPATH before running. That is not incidental: a
# sourced ROS 2 workspace puts its own site-packages on PYTHONPATH, and its
# pytest plugins get auto-loaded into unrelated projects, where they fail on
# imports they cannot satisfy. Clearing it here means `make test` works whether
# or not you have sourced a ROS setup in that shell.

PY := .venv/bin/python
PIP := .venv/bin/pip
RUN := env -u PYTHONPATH $(PY)

.PHONY: help venv install test doctor boards handshake verify-render costs \
        digest digest-dry preview clean

help:
	@echo "  make install        create the venv and install dependencies"
	@echo "  make doctor         report what is configured and what is missing"
	@echo "  make test           run the test suite"
	@echo "  make preview        show today's Summer 2027 postings (no credentials needed)"
	@echo "  make boards         verify every board token in companies.yml resolves"
	@echo "  make verify-render  prove the renderer reproduces your master resume"
	@echo "  make costs          measure what a day of digests costs"
	@echo "  make handshake      test your Handshake session cookie (off by default)"
	@echo "  make digest-dry     build the digest and print it, sending nothing"
	@echo "  make digest         build and send the digest"

venv:
	@test -d .venv || python3 -m venv .venv

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

test:
	$(RUN) -m pytest

# `|| true` because this is a report, not a gate: a non-zero exit here means
# "something is unconfigured", which is the normal state during setup and
# should not read as a build failure. CI calls `main.py --doctor` directly
# when it wants the exit code to matter.
doctor:
	@$(RUN) main.py --doctor || true

boards:
	$(RUN) main.py --check-boards

handshake:
	@$(RUN) main.py --check-handshake || true

preview:
	$(RUN) main.py --limit 25

digest-dry:
	$(RUN) main.py --digest --dry-run

digest:
	$(RUN) main.py --digest

clean:
	rm -rf .pytest_cache **/__pycache__ out/

verify-render:
	@$(RUN) -c "import verify_render, sys; sys.exit(verify_render.run())" || true

costs:
	@$(RUN) -c "import costs, sys; sys.exit(costs.run())" || true
