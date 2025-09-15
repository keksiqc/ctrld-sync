#! /usr/bin/env bash

# Update apk
sudo apk update
sudo apk upgrade

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Dependencies
uv sync

# Install pre-commit hooks
uv run pre-commit install --install-hooks
