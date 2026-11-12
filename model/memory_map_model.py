"""메모리 맵 JSON 스키마와 읽기 요청(Modbus FC03/FC04 프레임 단위) 모델.

- addr: 0-based 레지스터 번호. "100.5" 는 비트 하나, "100.8~F" 는 비트 구간 (0 = LSB, 10~15 는 A~F).
  "100.L" / "100.H" 는 하위 / 상위 바이트로 "100.0~7" / "100.8~F" 와 같다.
  HI6 S relay 표기 "S0100"(block), "S0100.3"(block 안의 비트)도 쓸 수 있고 s_relay_base 로 레지스터를 구한다.
  레지스터 단위 타입은 짝수 S 에서 시작해야 한다.
- type: u8 i8 bit 는 비트 구간으로만, u16 ~ f64 는 레지스터 단위로 지정한다.
  레지스터 단위 타입은 "i16[6]" 처럼 연속 배열로 쓸 수 있다. str 은 레지스터 타입이 아니고 literals 에서만 쓴다.
- scale / offset: 디코딩 값에 `값 × scale + offset` 을 적용한다. hex / bin 은 둘 다 적용하지 않는다.
- fields: u16/i16 배열 포인트를 워드 인덱스로 잘라 절대 주소 포인트로 내린다. 그 배열 포인트는 샘플로 나가지 않는다.
- literals: 레지스터가 아닌 시각 문자열과 상수.
- format: dec | hex | bin. hex / bin 은 scale 없이 원시 비트를 문자열로 낸다.
- sockets: command 를 써 두면 남은 자리에 result 를 채워 주는 영역(HI6 S relay socket)의 배치.
  위치는 S relay block 번호로 적고 s_relay_base 로 Modbus 레지스터를 구한다.
  layout 은 적지 않고 command 카탈로그(model.catalog_model)에서 가져온다.
"""
import re
import time
from collections import Counter
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator, model_validator

# FC03/FC04 한 요청에서 읽을 수 있는 최대 레지스터 수
MAX_READ_REGISTERS = 125
MAX_REGISTER = 0xFFFF
# 소켓 한 자리의 크기. block 은 8bit 단위이고 2 block = 1 word = 레지스터 1개다.
# command + params + result 가 모두 이 안에 들어간다
SOCKET_BLOCKS = 20
SOCKET_REGISTERS = SOCKET_BLOCKS // 2

_ADDR_RE = re.compile(r"^(\d+)(?:\.(?:([0-9A-F])(?:~([0-9A-F]))?|([LH])))?$", re.IGNORECASE)
# HI6 S relay 표기. S0100 은 block(8bit) 하나, S0100.3 은 그 block 의 비트 하나
_S_ADDR_RE = re.compile(r"^S(\d+)(?:\.([0-7]))?$", re.IGNORECASE)
# MemoryMapSettings 검증 context 로 S 표기 변환에 쓸 s_relay_base 를 넘기는 키
S_RELAY_BASE_CONTEXT = "s_relay_base"
_BYTE_BITS = {"L": (0, 7), "H": (8, 15)}
_TYPE_RE = re.compile(r"^([a-z]+\d*)(?:\[(\d+)\])?$")
# 생성되는 포인트 이름이 "{socket}.{point}" 이므로 구분자와 겹치는 문자는 막는다
_SOCKET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class RegisterKind(str, Enum):
    HOLDING = "holding"
    INPUT = "input"
    # 레지스터 읽기가 아니다. literals 샘플에만 쓴다
    LITERAL = "literal"


class WordEndian(str, Enum):
    # 앞 레지스터가 하위 워드 (collector-plc 와 동일)
    LE = "le"
    BE = "be"


class ValueFormat(str, Enum):
    DEC = "dec"
    # hex / bin 은 scale 을 적용하지 않은 원시 비트를 "0xFF38" / "0b00000101" 처럼 낸다
    HEX = "hex"
    BIN = "bin"


class PointType(str, Enum):
    BIT = "bit"
    U8 = "u8"
    I8 = "i8"
    U16 = "u16"
    I16 = "i16"
    U32 = "u32"
    I32 = "i32"
    U64 = "u64"
    I64 = "i64"
    F32 = "f32"
    F64 = "f64"
    # 레지스터 폭이 없다. literals 샘플의 type 으로만 쓴다
    STR = "str"

    @property
    def width(self) -> int:
        return _TYPE_SPECS[self][0]

    @property
    def struct_format(self) -> str | None:
        """None 이면 레지스터 안의 비트 구간으로 읽는 타입."""
        return _TYPE_SPECS[self][1]


_TYPE_SPECS: dict[PointType, tuple[int, str | None]] = {
    PointType.BIT: (1, None),
    PointType.U8: (8, None),
    PointType.I8: (8, None),
    PointType.U16: (16, "H"),
    PointType.I16: (16, "h"),
    PointType.U32: (32, "I"),
    PointType.I32: (32, "i"),
    PointType.U64: (64, "Q"),
    PointType.I64: (64, "q"),
    PointType.F32: (32, "f"),
    PointType.F64: (64, "d"),
    PointType.STR: (0, None),
}
# fields 가 자를 수 있는 원소. 1워드라 at 이 워드 인덱스와 같다
_WORD_SOURCES = {PointType.U16, PointType.I16}
# fields.type 에 허용하는 스칼라. 반복은 names 가 담당한다
_FIELD_TYPES = {
    PointType.U16, PointType.I16, PointType.U32, PointType.I32,
    PointType.U64, PointType.I64, PointType.F32, PointType.F64,
}


class MemoryMapPoint(BaseModel):
    """메모리 맵 한 점. addr/type 문자열은 검증 시 레지스터 위치·타입·길이로 풀어 둔다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    addr: str
    type: str
    scale: float | None = None
    # 디코딩 뒤 더한다. None 과 0 은 더하지 않아 정수 타입이 그대로 남는다
    offset: float | None = None
    # None 이면 맵의 format 을 따른다
    format: ValueFormat | None = None

    _register: int = PrivateAttr()
    _point_type: PointType = PrivateAttr()
    _length: int = PrivateAttr()
    _bits: tuple[int, int] | None = PrivateAttr()

    @field_validator("addr", "type", mode="before")
    @classmethod
    def _to_str(cls, value: object) -> str:
        return str(value).strip()

    @model_validator(mode="after")
    def _parse(self, info: ValidationInfo) -> "MemoryMapPoint":
        # 이미 풀어 둔 인스턴스를 ReadRequest 등 다른 모델에 넘기면 context 없이 이 검증이 다시 돈다
        if "_register" in (self.__pydantic_private__ or {}):
            return self
        m_type = _TYPE_RE.match(self.type.lower())
        if not m_type or m_type.group(1) not in PointType._value2member_map_:
            raise ValueError(f"{self.name}: 알 수 없는 type {self.type!r} (가능: {', '.join(t.value for t in PointType)})")
        point_type = PointType(m_type.group(1))
        if point_type is PointType.STR:
            raise ValueError(f"{self.name}: str 은 레지스터 타입이 아니다. literals 에 적는다")
        length = int(m_type.group(2) or 1)

        addr = self.addr
        m_s = _S_ADDR_RE.match(addr)
        if m_s:
            addr = self._s_to_register(m_s, point_type, (info.context or {}).get(S_RELAY_BASE_CONTEXT))
        m_addr = _ADDR_RE.match(addr)
        if not m_addr:
            raise ValueError(f"{self.name}: addr 형식 오류 {self.addr!r} (예: 100, 100.5, 100.8~F, 100.H, S0100, S0100.3)")

        register = int(m_addr.group(1))
        bits = None
        if m_addr.group(2) is not None or m_addr.group(4) is not None:
            if m_addr.group(4) is not None:
                lo, hi = _BYTE_BITS[m_addr.group(4).upper()]
            else:
                lo = int(m_addr.group(2), 16)
                hi = int(m_addr.group(3), 16) if m_addr.group(3) is not None else lo
            if hi < lo or hi - lo + 1 != point_type.width or point_type.struct_format or length != 1:
                raise ValueError(f"{self.name}: 비트 구간 {self.addr!r} 은 bit(1비트) / u8·i8(8비트) 단일 값에만 쓴다")
            bits = (lo, hi)
        elif not point_type.struct_format:
            raise ValueError(f"{self.name}: {point_type.value} 은 비트 구간으로 지정한다 (예: {register}.8~F, {register}.H)")
        if length < 1:
            raise ValueError(f"{self.name}: 배열 길이는 1 이상: {self.type!r}")

        self._register, self._point_type, self._length, self._bits = register, point_type, length, bits
        if self.end - 1 > MAX_REGISTER:
            raise ValueError(f"{self.name}: 레지스터 범위 초과 ({register}~{self.end - 1})")
        return self

    def _s_to_register(self, m: re.Match[str], point_type: PointType, base: object) -> str:
        """S 표기를 레지스터 표기로 바꾼다. 짝수 S 는 .L(bit 0~7), 홀수 S 는 .H(bit 8~F)."""
        if not isinstance(base, int):
            raise ValueError(f"{self.name}: S 표기 {self.addr!r} 를 쓰려면 s_relay_base(HI6 는 24000)가 필요하다")
        s_block, bit = int(m.group(1)), m.group(2)
        register, low = base + s_block // 2, s_block % 2 * 8
        if bit is not None:
            return f"{register}.{low + int(bit):X}"
        if point_type.width == 8 and not point_type.struct_format:
            return f"{register}.{'H' if low else 'L'}"
        if point_type is PointType.BIT:
            raise ValueError(f"{self.name}: bit 는 S0100.3 처럼 block 안의 비트(0~7)까지 적는다")
        if low:
            raise ValueError(
                f"{self.name}: {point_type.value} 는 레지스터 단위라 짝수 S 에서 시작해야 한다 ({self.addr!r}). "
                "레지스터에 걸친 값은 sockets 로 읽는다"
            )
        return str(register)

    @property
    def register(self) -> int:
        return self._register

    @property
    def point_type(self) -> PointType:
        return self._point_type

    @property
    def length(self) -> int:
        """배열 길이. 스칼라는 1."""
        return self._length

    @property
    def bits(self) -> tuple[int, int] | None:
        """비트 구간 (lo, hi). None 이면 레지스터 단위 타입."""
        return self._bits

    @property
    def register_count(self) -> int:
        return 1 if self._bits else self._point_type.width // 16 * self._length

    @property
    def end(self) -> int:
        return self._register + self.register_count


class SocketEntry(BaseModel):
    """소켓 한 자리의 배치. command 이름과 parameter 값만 적고 layout 은 카탈로그에서 가져온다.

    s_block 은 장비 매뉴얼의 S relay block 번호(S2000 -> 2000)다. Modbus 레지스터로는
    s_relay_base + s_block // 2 이고, 그 자리 20 block 안에 command·parameter·result 가 카탈로그 offset 대로 놓인다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    s_block: int = Field(ge=0)
    # 카탈로그의 command_name
    command: str
    # 카탈로그 parameter 순서대로의 값. hex 는 "0xFF" 처럼 문자열로 쓴다
    params: tuple[int | float | str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "SocketEntry":
        if not _SOCKET_NAME_RE.match(self.name):
            raise ValueError(f"소켓 이름은 영숫자/_/- 만 쓴다: {self.name!r}")
        if self.s_block % 2:
            raise ValueError(f"소켓 {self.name}: s_block 은 레지스터 경계인 짝수여야 한다 (S{self.s_block:04d})")
        return self


class FieldCut(BaseModel):
    """배열 포인트를 워드 인덱스로 자르는 규칙. 로드 시 절대 주소 포인트로 내려간다.

    source 는 holding / input 의 u16·i16 배열 이름이다. at 은 그 배열의 워드 인덱스이고,
    names 한 칸이 type 하나다. i32 면 이름마다 2워드씩 나아간다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    at: int = Field(default=0, ge=0)
    type: str
    names: list[str] = Field(min_length=1)
    scale: float | None = None
    offset: float | None = None
    format: ValueFormat | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _type_str(cls, value: object) -> str:
        return str(value).strip()


class LiteralOrigin(str, Enum):
    CLOCK = "clock"
    CONST = "const"


class LiteralSpec(BaseModel):
    """레지스터에서 오지 않는 샘플. clock 은 읽은 시각의 형식, const 는 문자열 상수."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str
    origin: LiteralOrigin = Field(alias="from")
    format: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "LiteralSpec":
        if self.origin is LiteralOrigin.CLOCK:
            if not self.format or self.value is not None:
                raise ValueError(f"literal {self.name}: clock 은 format 만 적는다")
            try:
                time.strftime(self.format)
            except ValueError as e:
                raise ValueError(f"literal {self.name}: format {self.format!r} 을 시각으로 쓸 수 없다") from e
        elif self.value is None or self.format is not None:
            raise ValueError(f"literal {self.name}: const 는 value 만 적는다")
        return self

    @property
    def addr(self) -> str:
        """포워더가 노드를 찾는 고정 주소. clock:이름 / const:이름."""
        return f"{self.origin.value}:{self.name}"


class MemoryMapSettings(BaseModel):
    """메모리 맵 JSON 최상위."""

    model_config = ConfigDict(extra="forbid")

    word_endian: WordEndian = WordEndian.LE
    # 빈 번지가 이 개수 이하면 같은 요청으로 묶는다
    max_gap: int = Field(default=8, ge=0)
    # 한 번의 FC03/FC04 가 읽는 레지스터 수 상한. 장비 제한이 125 보다 작을 때 쓴다
    max_read: int = Field(default=MAX_READ_REGISTERS, ge=1, le=MAX_READ_REGISTERS)
    format: ValueFormat = ValueFormat.DEC
    # S relay S0000 이 놓인 Modbus holding 레지스터 (HI6: 24000). sockets 나 addr 의 S 표기를 쓸 때 필요하다
    s_relay_base: int | None = Field(default=None, ge=0, le=MAX_REGISTER)
    holding: list[MemoryMapPoint] = Field(default_factory=list)
    input: list[MemoryMapPoint] = Field(default_factory=list)
    sockets: list[SocketEntry] = Field(default_factory=list)
    fields: list[FieldCut] = Field(default_factory=list)
    literals: list[LiteralSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "MemoryMapSettings":
        self._lower_fields()
        if self.sockets and self.s_relay_base is None:
            raise ValueError("sockets 를 쓰려면 s_relay_base(S0000 의 Modbus 레지스터, HI6 는 24000)가 필요하다")
        s_inputs = [p.name for p in self.input if _S_ADDR_RE.match(p.addr)]
        if s_inputs:
            raise ValueError(f"S relay 는 holding 영역이라 input 에 S 표기를 쓸 수 없다: {s_inputs}")
        for label, names in (
            ("name", [p.name for p in self.holding + self.input] + [lit.name for lit in self.literals]),
            ("소켓 name", [s.name for s in self.sockets]),
        ):
            duplicated = sorted({n for n, count in Counter(names).items() if count > 1})
            if duplicated:
                raise ValueError(f"{label} 중복: {duplicated}")
        return self

    def _lower_fields(self) -> None:
        """fields 를 절대 주소 포인트로 바꾸고, 소스로 쓰인 배열 포인트는 샘플에서 뺀다."""
        if not self.fields:
            return
        located: dict[str, tuple[RegisterKind, MemoryMapPoint]] = {}
        for kind, points in ((RegisterKind.HOLDING, self.holding), (RegisterKind.INPUT, self.input)):
            for point in points:
                located[point.name] = (kind, point)
        used: set[str] = set()
        extra: dict[RegisterKind, list[MemoryMapPoint]] = {RegisterKind.HOLDING: [], RegisterKind.INPUT: []}
        for field in self.fields:
            found = located.get(field.source)
            if found is None:
                raise ValueError(f"fields source 없음: {field.source!r}")
            kind, source = found
            if source.bits or source.point_type not in _WORD_SOURCES:
                raise ValueError(f"fields source {field.source!r} 는 u16 또는 i16 배열이어야 한다")
            element = _field_element(field)
            words = element.register_count
            span = len(field.names) * words
            if field.at + span > source.register_count:
                raise ValueError(
                    f"fields {field.source} at {field.at}: {span}워드가 "
                    f"{source.type}({source.register_count}워드)를 넘는다"
                )
            for index, name in enumerate(field.names):
                extra[kind].append(MemoryMapPoint(
                    name=name,
                    addr=str(source.register + field.at + index * words),
                    type=field.type,
                    scale=field.scale,
                    offset=field.offset,
                    format=field.format,
                ))
            used.add(field.source)
        self.holding = [p for p in self.holding if p.name not in used] + extra[RegisterKind.HOLDING]
        self.input = [p for p in self.input if p.name not in used] + extra[RegisterKind.INPUT]

    def points_of(self, kind: RegisterKind) -> list[MemoryMapPoint]:
        if kind is RegisterKind.HOLDING:
            return self.holding
        if kind is RegisterKind.INPUT:
            return self.input
        return []


def _field_element(field: FieldCut) -> MemoryMapPoint:
    """fields.type 이 스칼라 레지스터 타입인지 확인한다. 반복 개수는 names 길이다."""
    matched = _TYPE_RE.match(field.type.lower())
    if not matched or matched.group(2) or matched.group(1) not in PointType._value2member_map_:
        raise ValueError(
            f"fields {field.names[0]}: type {field.type!r} 은 스칼라 레지스터 타입이어야 한다 (반복은 names)"
        )
    point_type = PointType(matched.group(1))
    if point_type not in _FIELD_TYPES:
        raise ValueError(
            f"fields {field.names[0]}: type {field.type!r} 은 스칼라 레지스터 타입이어야 한다 (반복은 names)"
        )
    return MemoryMapPoint(name=field.names[0], addr="0", type=field.type, scale=field.scale, offset=field.offset)


class ReadRequest(BaseModel):
    """한 번의 FC03/FC04 요청으로 읽을 범위와 그 안에 든 포인트."""

    model_config = ConfigDict(frozen=True)

    kind: RegisterKind
    start: int
    count: int
    points: tuple[MemoryMapPoint, ...]

    @property
    def end(self) -> int:
        return self.start + self.count
