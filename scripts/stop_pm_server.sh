#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="private-pm.service"

echo "Stopping ${SERVICE_NAME} ..."
sudo systemctl stop "${SERVICE_NAME}"
echo "Done. Status:"
sudo systemctl status "${SERVICE_NAME}" --no-pager || true
