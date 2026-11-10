"""모든 환경 변수는 여기서만 읽는다. 저장소 루트의 .env 를 먼저 불러온다 (.env.example 참고).

    from config import settings
    client = ModbusTcpClient(settings.modbus_tcp)
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from core.modbus_client.config import ModbusRtuConfig, ModbusTcpConfig
from core.zeromq_client.config import ZmqConfig

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _env_int(key: str, default: int) -> int:
    return int(_env_str(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(_env_str(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, str(default)).lower() in ("1", "true", "yes", "on")


def _as_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def _env_path(key: str, default: str) -> Path:
    return _as_path(_env_str(key, default))


def _env_paths(key: str, default: str) -> tuple[Path, ...]:
    """쉼표로 여러 경로를 받는다. 첫 번째가 기준 파일이다."""
    paths = tuple(_as_path(p) for p in (s.strip() for s in _env_str(key, default).split(",")) if p)
    if not paths:
        raise ValueError(f"{key} 에 경로가 없다")
    return paths


def _env_optional_path(key: str) -> Path | None:
    value = _env_str(key, "")
    return _as_path(value) if value else None


@dataclass(slots=True)
class Settings:
    log_level: str
    # 프로토콜 collector_address / device_key, ZeroMQ 토픽 collector.robot.{device_key}.* 세그먼트
    device_key: str
    # data / health 헤더의 gateway_address. ack 는 요청 헤더 값을 에코한다
    gateway_address: str
    # tcp | rtu
    modbus_mode: str
    # 여러 개면 순서대로 합친다. 최상위 설정은 첫 번째(기준) 파일만 쓴다
    memory_map_files: tuple[Path, ...]
    # 소켓 command 카탈로그 CSV. 비우면 profiles/hi6 카탈로그
    command_catalog_file: Path | None
    poll_interval: float
    reconnect_interval: float
    modbus_tcp: ModbusTcpConfig
    modbus_rtu: ModbusRtuConfig
    zmq: ZmqConfig


def load_settings() -> Settings:
    modbus_mode = _env_str("MODBUS_MODE", "tcp").lower()
    if modbus_mode not in ("tcp", "rtu"):
        raise ValueError(f"MODBUS_MODE 는 tcp 또는 rtu: {modbus_mode!r}")
    device_key = _env_str("DEVICE_KEY", "robot-1")
    if "." in device_key:
        raise ValueError(f"DEVICE_KEY 에 '.' 는 쓸 수 없다 (토픽 계층 구분자): {device_key!r}")
    tcp, rtu, zmq = ModbusTcpConfig(), ModbusRtuConfig(), ZmqConfig()
    return Settings(
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        device_key=device_key,
        gateway_address=_env_str("GATEWAY_ADDRESS", "gateway"),
        modbus_mode=modbus_mode,
        memory_map_files=_env_paths(
            "MEMORY_MAP_FILE", "profiles/hi6/hi6_s_relay_memory_map.json"
        ),
        command_catalog_file=_env_optional_path("COMMAND_CATALOG_FILE")
        or _as_path("profiles/hi6/hi6_s_relay_command_catalog.csv"),
        poll_interval=_env_float("POLL_INTERVAL_SEC", 1.0),
        reconnect_interval=_env_float("RECONNECT_INTERVAL_SEC", 5.0),
        modbus_tcp=ModbusTcpConfig(
            device_id=_env_int("MODBUS_TCP_DEVICE_ID", tcp.device_id),
            timeout=_env_float("MODBUS_TCP_TIMEOUT", tcp.timeout),
            retries=_env_int("MODBUS_TCP_RETRIES", tcp.retries),
            host=_env_str("MODBUS_TCP_HOST", tcp.host),
            port=_env_int("MODBUS_TCP_PORT", tcp.port),
        ),
        modbus_rtu=ModbusRtuConfig(
            device_id=_env_int("MODBUS_RTU_DEVICE_ID", rtu.device_id),
            timeout=_env_float("MODBUS_RTU_TIMEOUT", rtu.timeout),
            retries=_env_int("MODBUS_RTU_RETRIES", rtu.retries),
            port=_env_str("MODBUS_RTU_PORT", rtu.port),
            baudrate=_env_int("MODBUS_RTU_BAUDRATE", rtu.baudrate),
            bytesize=_env_int("MODBUS_RTU_BYTESIZE", rtu.bytesize),
            parity=_env_str("MODBUS_RTU_PARITY", rtu.parity).upper(),
            stopbits=_env_int("MODBUS_RTU_STOPBITS", rtu.stopbits),
            handle_local_echo=_env_bool("MODBUS_RTU_LOCAL_ECHO", rtu.handle_local_echo),
        ),
        zmq=ZmqConfig(
            pub_endpoint=_env_str("ZMQ_PUB_ENDPOINT", zmq.pub_endpoint),
            sub_endpoint=_env_str("ZMQ_SUB_ENDPOINT", zmq.sub_endpoint),
            pub_bind=_env_bool("ZMQ_PUB_BIND", zmq.pub_bind),
            sub_bind=_env_bool("ZMQ_SUB_BIND", zmq.sub_bind),
            recv_timeout_ms=_env_int("ZMQ_RECV_TIMEOUT_MS", zmq.recv_timeout_ms),
            linger_ms=_env_int("ZMQ_LINGER_MS", zmq.linger_ms),
        ),
    )


settings = load_settings()
