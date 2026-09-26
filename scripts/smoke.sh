#!/usr/bin/env bash
# Phase 0 smoke check. Later phases extend this script; they are not invoked here.
set -euo pipefail
cd "$(dirname "$0")/.."
govcon db upgrade
govcon db seed-demo-watchlist
govcon status
