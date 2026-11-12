"""JSON 메모리 맵으로 읽기 요청을 자동으로 묶고 값을 디코딩한다 (쓰기용 encode 도 제공). 스키마는 model.memory_map_model 참고.

    {
      "word_endian": "le",
      "max_gap": 8,
      "format": "dec",
      "holding": [
        {"addr": "0.8~F", "type": "u8",     "name": "status_code"},
        {"addr": "1",     "type": "i16",    "name": "temperature", "scale": 0.1, "offset": -40},
        {"addr": "5",     "type": "i16[6]", "name": "joint_torque"},
        {"addr": "11",    "type": "u32",    "name": "run_counter", "format": "hex"},
        {"addr": "20",    "type": "u16[8]", "name": "raw"}
      ],
      "input": [{"addr": "100", "type": "f32", "name": "battery_voltage"}],
      "fields": [
        {"source": "raw", "at": 2, "type": "i32", "names": ["J13", "J14", "J15"]}
      ],
      "literals": [
        {"name": "Timestamp", "from": "clock", "format": "%Y.%m.%d.%H:%M:%S"},
        {"name": "Model", "from": "const", "value": "igus_DLE-DR-0001"}
      ]
    }

- word_endian: 2레지스터 이상 값의 순서. le = 앞 레지스터가 하위 워드 (collector-plc 와 동일).
- max_gap: 빈 번지가 이 개수 이하면 같은 요청으로 묶는다. 장비가 범위를 거부하면 자동으로 쪼개 다시 읽는다.
- format: dec | hex | bin. 포인트별로 덮어쓸 수 있다. hex / bin 은 scale·offset 을 적용하지 않은 원시 비트를
  "0xFF38" / "0b00000101" 처럼 낸다.
- scale / offset: 디코딩 값에 `값 × scale + offset`. None 과 0 은 그 항을 건너뛴다.
- fields: u16/i16 배열 포인트를 워드 인덱스로 잘라 절대 주소 포인트로 내린다. 배열 포인트 자체는 샘플이 아니다.
- literals: 읽기가 성공한 뒤에 붙는 시각 문자열과 상수. 주소는 clock:이름 / const:이름.
- sockets: HI6 S relay socket 배치. 카탈로그로 펼치는 일은 core.modbus_client.sockets 가 한다.
"""
import itertools
import json
import logging
import struct
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from model import (
    MAX_READ_REGISTERS,
    S_RELAY_BASE_CONTEXT,
    SOCKET_BLOCKS,
    CommandCatalog,
    LiteralOrigin,
    MemoryMapPoint,
    MemoryMapSettings,
    PointType,
    ReadRequest,
    RegisterKind,
    Sample,
    SocketEntry,
    SocketSample,
    SocketSetup,
    Value,
    ValueFormat,
    WordEndian,
)

from .base_client import BaseModbusClient
from .exceptions import ModbusClientError, ModbusErrorCode
from .sockets import (
    blocks_to_registers,
    describe_socket,
    expand_socket,
    read_socket,
    registers_to_blocks,
    setup_mismatch,
    write_socket,
)

logger = logging.getLogger(__name__)

_READERS = {
    RegisterKind.HOLDING: BaseModbusClient.read_holding_registers,
    RegisterKind.INPUT: BaseModbusClient.read_input_registers,
}
_SIGNED_TYPES = {PointType.I8, PointType.I16, PointType.I32, PointType.I64}
# 맵 전체에 걸리는 설정. 파일을 여러 개로 나눌 때는 첫 번째(기준) 파일에만 둔다
GLOBAL_KEYS = ("word_endian", "max_gap", "max_read", "format", "s_relay_base")
# 파일을 나눠 담을 수 있는 목록 키
LIST_KEYS = ("holding", "input", "sockets", "fields", "literals")
# 레지스터를 읽는 영역. literal 은 읽기 계획에 넣지 않는다
_POINT_KINDS = (RegisterKind.HOLDING, RegisterKind.INPUT)


class MemoryMap:
    def __init__(self, settings: MemoryMapSettings, catalog: CommandCatalog | None = None) -> None:
        self.settings = settings
        self.catalog = catalog
        self.sockets = tuple(self._expand_socket(e) for e in settings.sockets)
        _check_sockets(self.sockets, settings)
        self.plan = tuple(
            r for kind in _POINT_KINDS for r in _plan(kind, settings.points_of(kind), settings.max_gap, settings.max_read)
        )
        # 붙어 있는 소켓은 한 요청으로 읽는다
        self.socket_plan = _plan_sockets(self.sockets)

    @classmethod
    def from_json(cls, *paths: str | Path, catalog: CommandCatalog | None = None) -> "MemoryMap":
        """맵 파일 하나 또는 여러 개를 읽어 합친다. 경로 순서가 곧 우선순위다 (첫 번째가 기준 파일)."""
        if not paths:
            raise ValueError("메모리 맵 경로가 없다")
        return cls.from_dict(_merge([Path(p) for p in paths]), catalog=catalog)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], catalog: CommandCatalog | None = None) -> "MemoryMap":
        # 포인트 addr 의 S 표기(S0100)는 같은 맵의 s_relay_base 로 레지스터를 구한다
        context = {S_RELAY_BASE_CONTEXT: raw.get("s_relay_base")}
        return cls(MemoryMapSettings.model_validate(raw, context=context), catalog=catalog)

    def read(self, client: BaseModbusClient) -> list[Sample]:
        """계획된 요청으로 holding / input 포인트를 읽는다. 연결 계열 에러만 예외로 올리고 나머지는 Sample.error 에 담는다."""
        samples: dict[str, Sample] = {}
        plan: list[ReadRequest] = []
        for request in self.plan:
            plan += self._execute(client, request, samples)
        # 범위 거부로 쪼갠 계획은 다음 읽기부터 그대로 쓴다
        self.plan = tuple(plan)
        return [samples[p.name] for kind in _POINT_KINDS for p in self.settings.points_of(kind)]

    def literal_samples(self, sample_ms: int) -> list[Sample]:
        """레지스터 읽기가 성공한 뒤에 붙일 시각·상수 샘플. 시각은 수집 호스트의 로컬 시각이다."""
        samples: list[Sample] = []
        for spec in self.settings.literals:
            if spec.origin is LiteralOrigin.CLOCK:
                value = time.strftime(spec.format or "", time.localtime(sample_ms / 1000))
            else:
                value = spec.value or ""
            samples.append(Sample(
                name=spec.name, kind=RegisterKind.LITERAL, addr=spec.addr, type=PointType.STR, value=value,
            ))
        return samples

    def read_sockets(self, client: BaseModbusClient) -> list[SocketSample]:
        """소켓마다 result 묶음 하나를 낸다. 연결 계열 에러만 예외로 올리고 나머지는 SocketSample.error 에 담는다.

        command / parameter 자리까지 같이 읽으므로 매 읽기가 곧 전제조건 확인이다.
        기대값과 다르면(재부팅으로 초기화 등) 다시 쓰고, 그 소켓은 이번 읽기만 E-3101 로 낸다.
        """
        results: dict[str, SocketSample] = {}
        socket_plan: list[tuple[SocketSetup, ...]] = []
        for group in self.socket_plan:
            socket_plan += self._read_sockets(client, group, results)
        self.socket_plan = tuple(socket_plan)
        return [results[s.name] for s in self.sockets]

    @staticmethod
    def values(samples: list[Sample]) -> dict[str, Value]:
        """성공한 포인트만 {name: value} 로 모은다."""
        return {s.name: s.value for s in samples if s.error is None}

    def describe_plan(self) -> str:
        """실제로 나갈 읽기 요청과 소켓 배치. 장비 없이 설정을 확인할 때 쓴다."""
        lines = [
            f"{r.kind.value} {r.start}~{r.end - 1} ({r.count} regs): {', '.join(p.name for p in r.points)}"
            for r in self.plan
        ]
        lines += [
            f"holding {g[0].base}~{g[-1].end - 1} ({g[-1].end - g[0].base} regs): sockets {', '.join(s.name for s in g)}"
            for g in self.socket_plan
        ]
        lines += [describe_socket(s) for s in self.sockets]
        lines += [
            f"literal {spec.name} {spec.addr} = {spec.format if spec.origin is LiteralOrigin.CLOCK else spec.value}"
            for spec in self.settings.literals
        ]
        return "\n".join(lines)

    def decode(self, point: MemoryMapPoint, registers: list[int]) -> Value:
        """포인트가 차지하는 레지스터만 받아 값으로 바꾼다."""
        if point.bits:
            lo, hi = point.bits
            return self._convert(point, (registers[0] >> lo) & ((1 << (hi - lo + 1)) - 1))
        words_per_item = point.point_type.width // 16
        items = []
        for i in range(point.length):
            words = registers[i * words_per_item : (i + 1) * words_per_item]
            if self.settings.word_endian is WordEndian.LE:
                words = words[::-1]
            items.append(self._convert(point, int.from_bytes(b"".join(w.to_bytes(2, "big") for w in words), "big")))
        return items[0] if point.length == 1 else items

    def encode(self, point: MemoryMapPoint, value: Value, current: list[int] | None = None) -> list[int]:
        """decode 의 역. 포인트가 차지하는 레지스터에 쓸 값을 만든다.

        비트 구간 포인트는 같은 레지스터의 나머지 비트를 보존해야 하므로 현재 레지스터 값(current)이 필요하다.
        "0xFF38" 같은 hex 문자열은 scale 없이 원시 비트로 쓴다.
        """
        items = value if isinstance(value, list) else [value]
        if len(items) != point.length:
            raise ValueError(f"{point.name}: 값 {len(items)}개, 필요 {point.length}개")
        if point.bits:
            if current is None:
                raise ValueError(f"{point.name}: 비트 구간 쓰기는 현재 레지스터 값이 필요하다")
            lo, hi = point.bits
            mask = ((1 << (hi - lo + 1)) - 1) << lo
            return [(current[0] & ~mask) | (self._to_raw(point, items[0]) << lo)]
        words_per_item = point.point_type.width // 16
        registers: list[int] = []
        for item in items:
            raw = self._to_raw(point, item)
            words = [(raw >> (16 * i)) & 0xFFFF for i in range(words_per_item)]
            registers += words if self.settings.word_endian is WordEndian.LE else words[::-1]
        return registers

    def _expand_socket(self, entry: SocketEntry) -> SocketSetup:
        if self.catalog is None:
            raise ValueError(f"소켓 {entry.name}: command 카탈로그가 없다 (COMMAND_CATALOG_FILE 을 설정한다)")
        # MemoryMapSettings 가 sockets 가 있으면 s_relay_base 를 요구한다
        return expand_socket(entry, self.catalog, self.settings.s_relay_base)

    def _read_sockets(
        self, client: BaseModbusClient, group: tuple[SocketSetup, ...], results: dict[str, SocketSample]
    ) -> list[tuple[SocketSetup, ...]]:
        start = group[0].base
        try:
            registers = client.read_holding_registers(start, group[-1].end - start)
        except ModbusClientError as e:
            if e.code.requires_reconnect:
                raise
            if e.code is ModbusErrorCode.ADDR_OUT_OF_RANGE and len(group) > 1:
                logger.warning("범위 거부로 소켓 요청 분할: holding %d~%d -> %d개", start, group[-1].end - 1, len(group))
                return [g for s in group for g in self._read_sockets(client, (s,), results)]
            for socket in group:
                results[socket.name] = read_socket(socket, error=e.code.value)
            return [group]
        for socket in group:
            offset = socket.base - start
            blocks = registers_to_blocks(registers[offset : offset + socket.end - socket.base])
            results[socket.name] = self._check_socket(client, socket, blocks)
        return [group]

    def _check_socket(self, client: BaseModbusClient, socket: SocketSetup, blocks: bytes) -> SocketSample:
        mismatch = setup_mismatch(socket, blocks)
        if not mismatch:
            return read_socket(socket, blocks)
        logger.warning(
            "소켓 %s(%s) command/parameter 불일치 S%04d~ 현재 %s, 기대 %s -> 다시 쓴다",
            socket.name, socket.s_range, socket.s_block + mismatch[0],
            blocks_to_registers(blocks)[: len(socket.setup_registers)], list(socket.setup_registers),
        )
        try:
            write_socket(client, socket)
        except ModbusClientError as e:
            if e.code.requires_reconnect:
                raise
            logger.error("소켓 %s 쓰기 실패: %s", socket.name, e)
            return read_socket(socket, blocks, error=e.code.value)
        return read_socket(socket, blocks, error=ModbusErrorCode.SOCKET_REWRITTEN.value)

    def _execute(self, client: BaseModbusClient, request: ReadRequest, samples: dict[str, Sample]) -> list[ReadRequest]:
        try:
            registers = _read_registers(client, request)
        except ModbusClientError as e:
            if e.code.requires_reconnect:
                raise
            parts = _plan(request.kind, request.points, -1, self.settings.max_read) if e.code is ModbusErrorCode.ADDR_OUT_OF_RANGE else []
            if len(parts) > 1:
                logger.warning(
                    "범위 거부로 요청 분할: %s %d~%d -> %d개",
                    request.kind.value, request.start, request.end - 1, len(parts),
                )
                return [r for part in parts for r in self._execute(client, part, samples)]
            for p in request.points:
                samples[p.name] = _sample(request.kind, p, error=e.code.value)
            return [request]
        for p in request.points:
            offset = p.register - request.start
            samples[p.name] = _sample(request.kind, p, value=self.decode(p, registers[offset : offset + p.register_count]))
        return [request]

    def _convert(self, point: MemoryMapPoint, raw: int) -> int | float | str:
        point_type = point.point_type
        width = point.bits[1] - point.bits[0] + 1 if point.bits else point_type.width
        fmt = point.format or self.settings.format
        if fmt is ValueFormat.HEX:
            return f"0x{raw:0{(width + 3) // 4}X}"
        if fmt is ValueFormat.BIN:
            return f"0b{raw:0{width}b}"
        if point_type.struct_format:
            value = struct.unpack(">" + point_type.struct_format, raw.to_bytes(width // 8, "big"))[0]
        elif point_type is PointType.I8 and raw & 0x80:
            value = raw - 0x100
        else:
            value = raw
        if point.scale is not None:
            value = value * point.scale
        if point.offset:
            value = value + point.offset
        return value

    def _to_raw(self, point: MemoryMapPoint, value: int | float | str) -> int:
        point_type = point.point_type
        width = point.bits[1] - point.bits[0] + 1 if point.bits else point_type.width
        if isinstance(value, str):
            text = value.strip().lower()
            # "0b101" 을 16진으로 읽으면 0xB101 이 되므로 접두사로 진법을 고른다
            raw = int(text, 2 if text.startswith("0b") else 16)
            if not 0 <= raw < 1 << width:
                raise ValueError(f"{point.name}: {value} 는 {width}비트를 넘는다")
            return raw
        if point.offset:
            value = value - point.offset
        if point_type in (PointType.F32, PointType.F64):
            scaled = value if point.scale is None else value / point.scale
            return int.from_bytes(struct.pack(">" + point_type.struct_format, scaled), "big")
        if point.scale is not None:
            value = round(value / point.scale)
        elif isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{point.name}: {point_type.value} 에 소수 {value} 는 쓸 수 없다")
        value = int(value)
        low = -(1 << (width - 1)) if point_type in _SIGNED_TYPES else 0
        if not low <= value < low + (1 << width):
            raise ValueError(f"{point.name}: {value} 는 {point_type.value} 범위({low}~{low + (1 << width) - 1}) 밖")
        return value & ((1 << width) - 1)


def _merge(paths: list[Path]) -> dict[str, Any]:
    """맵 파일들을 하나로 합친다. 최상위 설정은 기준 파일만 쓰고 목록 키는 이어 붙인다."""
    merged: dict[str, Any] = {key: [] for key in LIST_KEYS}
    for index, path in enumerate(paths):
        # Windows 편집기가 붙인 BOM 을 떼고 읽는다
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: 메모리 맵 최상위는 객체여야 한다")
        if index == 0:
            merged.update({k: v for k, v in raw.items() if k not in LIST_KEYS})
        else:
            # 조용히 한쪽이 이기면 추적할 수 없으므로 거부한다
            extra = [k for k in raw if k not in LIST_KEYS]
            if extra:
                raise ValueError(f"{path}: 최상위 설정 {extra} 는 기준 파일({paths[0].name})에만 둔다")
        for key in LIST_KEYS:
            merged[key] += raw.get(key) or []
    if len(paths) > 1:
        logger.info("memory map merged: %s", ", ".join(p.name for p in paths))
    return merged


def _check_sockets(sockets: tuple[SocketSetup, ...], settings: MemoryMapSettings) -> None:
    """소켓끼리 번지가 겹치면 서로의 설정을 덮어쓰므로 거부한다."""
    for a, b in itertools.combinations(sorted(sockets, key=lambda s: s.base), 2):
        if a.end > b.base:
            raise ValueError(
                f"소켓 {a.name}({a.s_range}) 와 {b.name}({b.s_range}) 의 번지가 겹친다 "
                f"(소켓 한 자리 = {SOCKET_BLOCKS} block 이므로 s_block 은 {SOCKET_BLOCKS} 이상 떨어져야 한다)"
            )
    # 읽기만 하는 포인트가 소켓 영역에 있어도 동작은 하지만 이름이 갈려 혼란스럽다
    for point in settings.holding:
        for socket in sockets:
            if socket.base <= point.register < socket.end:
                logger.warning(
                    "holding 포인트 %s(%s) 가 소켓 %s 영역(%s = holding %d~%d) 안에 있다",
                    point.name, point.addr, socket.name, socket.s_range, socket.base, socket.end - 1,
                )


def _sample(kind: RegisterKind, point: MemoryMapPoint, value: Value | None = None, error: str | None = None) -> Sample:
    return Sample(name=point.name, kind=kind, addr=point.addr, type=point.point_type, value=value, error=error)


def _plan_sockets(sockets: tuple[SocketSetup, ...]) -> tuple[tuple[SocketSetup, ...], ...]:
    groups: list[list[SocketSetup]] = []
    for socket in sorted(sockets, key=lambda s: s.base):
        if groups and groups[-1][-1].end == socket.base and socket.end - groups[-1][0].base <= MAX_READ_REGISTERS:
            groups[-1].append(socket)
        else:
            groups.append([socket])
    return tuple(tuple(g) for g in groups)


def _plan(kind: RegisterKind, points: Iterable[MemoryMapPoint], max_gap: int, max_read: int = MAX_READ_REGISTERS) -> list[ReadRequest]:
    """주소순 정렬 후, 간격이 max_gap 이하이고 max_read 레지스터를 넘지 않으면 한 요청으로 묶는다.

    max_gap=-1 이면 레지스터를 공유하는 포인트끼리만 묶는다 (범위 거부 시 분할용).
    """
    limit = min(max_read, MAX_READ_REGISTERS)
    requests: list[ReadRequest] = []
    group: list[MemoryMapPoint] = []
    start = end = 0
    for p in sorted(points, key=lambda p: p.register):
        if group and p.register - end <= max_gap and max(end, p.end) - start <= limit:
            group.append(p)
            end = max(end, p.end)
            continue
        if group:
            requests.append(ReadRequest(kind=kind, start=start, count=end - start, points=tuple(group)))
        group, start, end = [p], p.register, p.end
    if group:
        requests.append(ReadRequest(kind=kind, start=start, count=end - start, points=tuple(group)))
    return requests


def _read_registers(client: BaseModbusClient, request: ReadRequest) -> list[int]:
    read = _READERS[request.kind]
    registers: list[int] = []
    for start in range(request.start, request.end, MAX_READ_REGISTERS):
        registers += read(client, start, min(MAX_READ_REGISTERS, request.end - start))
    return registers
