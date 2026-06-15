#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="private-pm.service"

echo "Starting ${SERVICE_NAME} ..."
sudo systemctl start "${SERVICE_NAME}"
echo "Done. Status:"
sudo systemctl status "${SERVICE_NAME}" --no-pager || true
