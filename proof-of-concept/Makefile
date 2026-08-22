# Raccourcis de développement. L'installation sur la VM passe par
# scripts/install.sh, pas par ce fichier.

VENV := .venv
PY   := $(VENV)/bin/python

# Environnement de développement : base et fichiers générés dans ./var,
# rechargement d'Asterisk et synthèse vocale désactivés.
DEV_ENV := \
	TELEPHONIE_DATA_DIR=$(PWD)/var/data \
	TELEPHONIE_DB=$(PWD)/var/data/dev.db \
	TELEPHONIE_ASTERISK_DIR=$(PWD)/var/etc \
	TELEPHONIE_GENERATED_DIR=$(PWD)/var/etc/generated \
	TELEPHONIE_RELOAD=0 \
	TELEPHONIE_TTS=0

.PHONY: help venv test run seed clean

help:
	@echo "make venv   installe les dépendances dans $(VENV)"
	@echo "make test   lance la suite de tests"
	@echo "make seed   remplit une base de développement"
	@echo "make run    démarre l'interface sur http://127.0.0.1:8080"
	@echo "make clean  supprime le répertoire de travail local"

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -r requirements.txt pytest httpx

test:
	$(PY) -m pytest tests/ -q

seed:
	mkdir -p var/data var/etc/generated
	$(DEV_ENV) $(PY) scripts/seed.py

run:
	mkdir -p var/data var/etc/generated
	$(DEV_ENV) $(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

clean:
	rm -rf var
