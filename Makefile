
PREFIX  ?= $(HOME)/.local
BINDIR  ?= $(PREFIX)/bin
DATADIR ?= $(PREFIX)/share
PYTHON  ?= python3
APP_ID   = org.dancingmirrors.VamBackstage

SOABI   := $(shell $(PYTHON) -c 'import sysconfig; print(sysconfig.get_config_var("SOABI") or "cpython")')
APPDIR   = $(DATADIR)/vam-backstage
VENV    ?= $(APPDIR)/venv-$(SOABI)
export PIP_CACHE_DIR ?= $(APPDIR)/pip-cache-$(SOABI)

DESKTOP  = $(DATADIR)/applications/$(APP_ID).desktop
ICON     = $(DATADIR)/icons/hicolor/512x512/apps/$(APP_ID).png

.PHONY: all check run demo install uninstall clean lint format

all: check

check:
	$(PYTHON) -m pytest -q

run:
	$(PYTHON) -m vambackstage

demo:
	$(PYTHON) scripts/make_demo_library.py /tmp/vam-backstage-demo --packages 70
	$(PYTHON) -m vambackstage --data-dir /tmp/vam-backstage-demo-data --vam-dir /tmp/vam-backstage-demo

install:
	@$(PYTHON) -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')" 2>/dev/null \
	  || { echo "error: $(PYTHON) cannot import PyGObject with GTK 4 and libadwaita;"; \
	       echo "       install python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1 (or your distro's equivalents) first."; exit 1; }
	$(PYTHON) -m venv --system-site-packages "$(VENV)"
	"$(VENV)/bin/python" -m pip install --quiet --upgrade .
	install -d "$(BINDIR)"
	printf '%s\n' '#!/bin/sh' \
	  '# VaM Backstage launcher: picks the virtualenv built for the Python ABI in use' \
	  '# (a glibc container and a musl host can share this $$HOME).' \
	  'for py in "$(PYTHON)" python3 python; do' \
	  '  command -v "$$py" >/dev/null 2>&1 || continue' \
	  '  soabi=$$("$$py" -c "import sysconfig; print(sysconfig.get_config_var(\"SOABI\") or \"cpython\")" 2>/dev/null) || continue' \
	  '  venv="$(APPDIR)/venv-$$soabi"' \
	  '  [ -x "$$venv/bin/vam-backstage" ] && exec "$$venv/bin/vam-backstage" "$$@"' \
	  'done' \
	  'echo "vam-backstage: no installation for this Python ABI under $(APPDIR); run make install from this environment" >&2' \
	  'exit 1' > "$(BINDIR)/vam-backstage"
	chmod 755 "$(BINDIR)/vam-backstage"
	install -d "$(DATADIR)/applications"
	sed 's|^Exec=.*|Exec=$(BINDIR)/vam-backstage|' vambackstage/data/$(APP_ID).desktop > "$(DESKTOP)"
	chmod 644 "$(DESKTOP)"
	install -Dm644 vambackstage/data/icons/hicolor/512x512/apps/$(APP_ID).png "$(ICON)"
	-update-desktop-database -q "$(DATADIR)/applications" 2>/dev/null
	-gtk-update-icon-cache -q -t -f "$(DATADIR)/icons/hicolor" 2>/dev/null
	@echo "Installed: $(BINDIR)/vam-backstage"

uninstall:
	rm -rf "$(VENV)" "$(PIP_CACHE_DIR)"
	rm -f "$(BINDIR)/vam-backstage" "$(DESKTOP)" "$(ICON)"
	-update-desktop-database -q "$(DATADIR)/applications" 2>/dev/null
	-gtk-update-icon-cache -q -t -f "$(DATADIR)/icons/hicolor" 2>/dev/null

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find vambackstage tests scripts -name __pycache__ -type d -prune -exec rm -rf {} +

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m black --check .

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m black .
