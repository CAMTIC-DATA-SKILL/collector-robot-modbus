#!/usr/bin/env bash
# 현재 프로젝트 경로 기준으로 systemd 시스템 유닛을 등록·시작한다.
# 사용: sudo bash service_add.sh
set -euo pipefail

SERVICE_NAME="collector-robot-modbus-hh7"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_PYTHON="${ROOT}/.venv/bin/python"
MAIN_PY="${ROOT}/main.py"
RUN_USER="$(stat -c '%U' "${ROOT}")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "root 권한이 필요합니다. 예: sudo bash service_add.sh" >&2
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

if [[ ! -f "${ROOT}/hh7.env" ]]; then
  echo "경고: ${ROOT}/hh7.env 가 없습니다. hh7.env.example 을 복사해 설정을 맞추세요." >&2
fi

cat > "${UNIT_DST}" <<EOF
[Unit]
Description=Robot Modbus collector (southbound Modbus -> ZeroMQ PUB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}

WorkingDirectory=${ROOT}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${ROOT}/hh7.env
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
