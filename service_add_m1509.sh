#!/usr/bin/env bash
# 두산 M1509 수집기 유닛만 등록한다. collector-robot-modbus.service 는 건드리지 않는다.
# 사용: sudo bash service_add_m1509.sh
set -euo pipefail

SERVICE_NAME="collector-robot-modbus-m1509"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_PYTHON="${ROOT}/.venv/bin/python"
MAIN_PY="${ROOT}/main.py"
ENV_FILE="${ROOT}/m1509.env"
MAP_FILE="${ROOT}/profiles/m1509/m1509_memory_map.json"
RUN_USER="$(stat -c '%U' "${ROOT}")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root 권한이 필요합니다. 예: sudo bash service_add_m1509.sh" >&2
  exit 1
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo ".venv 가 없습니다: ${VENV_PYTHON}" >&2
  echo "먼저: python3.10 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "${MAIN_PY}" ]]; then
  echo "main.py 가 없습니다: ${MAIN_PY}" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "m1509.env 가 없습니다: ${ENV_FILE}" >&2
  exit 1
fi

if [[ ! -f "${MAP_FILE}" ]]; then
  echo "메모리 맵이 없습니다: ${MAP_FILE}" >&2
  exit 1
fi

cat > "${UNIT_DST}" <<EOF
[Unit]
Description=Robot Modbus collector m1509 (Doosan holding map -> ZeroMQ PUB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}

WorkingDirectory=${ROOT}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_PYTHON} ${MAIN_PY}

Restart=on-failure
RestartSec=5
TimeoutStopSec=30

# Modbus RTU 시리얼 포트 접근
SupplementaryGroups=dialout

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# 같은 이름 user 유닛이 있으면 내린다 (포트 충돌 방지)
systemctl --user -M "${RUN_USER}@" disable --now "${SERVICE_NAME}.service" 2>/dev/null || true

systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
