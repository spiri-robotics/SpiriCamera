# Minimal makefile for SpiriCamera documentation
#
# Usage:
#   make docs        # Build HTML docs
#   make clean       # Remove build artifacts
#   make watch       # Live-reload docs on file changes
#

PYTHON := python3
UV := uv
SPHINXBUILD := sphinx-build
SOURCEDIR := docs
BUILDDIR := $(SOURCEDIR)/_build

help:
	@echo "Please use 'make <target>' where <target> is one of"
	@echo "  docs       to build HTML docs"
	@echo "  clean      to remove build artifacts"
	@echo "  watch      to watch docs with sphinx-autobuild"

clean:
	@rm -rf $(BUILDDIR)/*

docs:
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)"

watch:
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)_watch" --watch .
	@python3 -m http.server --bind 127.0.0.1 --directory "$(BUILDDIR)_watch/html" 8888

.PHONY: help clean docs watch
