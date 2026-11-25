"""config/models.toml and its loader."""

from __future__ import annotations

import tomllib

import pytest

from common import model_settings


def test_every_use_in_models_toml_parses():
    with model_settings.MODELS_FILE.open("rb") as file:
        assert set(tomllib.load(file)) == set(model_settings.USES) | {"providers"}
    for use in model_settings.USES:
        model_settings.get(use)


def test_a_key_the_use_does_not_take_raises(models_file):
    models_file('[final]\nprovider = "vertex"\nmodel = "m"\nmax_tokens = 100\n')
    with pytest.raises(TypeError, match="max_tokens"):
        model_settings.get("final")
