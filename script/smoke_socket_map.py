"""소켓 command 카탈로그 / 메모리 맵 sockets 스모크 테스트. 장비 없이 돌고 Windows 에서도 실행된다.

인자가 없으면 docs/command_catalog.example.csv 로 block 코덱, S relay 주소 변환, 펼치기, 병합, 거부 규칙을 점검하고
profiles/*/ 의 카탈로그와 같은 폴더의 *memory_map*.json 이 읽히는지 본다.
--catalog / --map 을 주면 실제 파일을 읽어 검증하고 펼친 계획을 출력한다 (배포 전 확인용).

예) python script/smoke_socket_map.py
    python script/smoke_socket_map.py --catalog profiles/hi6/hi6_s_relay_command_catalog.csv --map memory_map.json sockets.json
"""
import argparse
import json
import logging
import math
import struct
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _smoke import ROOT, SmokeAbort, SmokeRunner, expect
from pydantic import ValidationError

from core.modbus_client import (
    MemoryMap,
    blocks_to_registers,
    decode_point,
    encode_point,
    load_catalog,
    registers_to_blocks,
)
from model import SOCKET_BLOCKS, CatalogPoint, CommandCatalog, CommandDef, PointType, RegisterKind

EXAMPLE_CATALOG = ROOT / "docs" / "command_catalog.example.csv"
PROFILES = ROOT / "profiles"
HI6_S_RELAY_BASE = 24000


def _sockets(*entries: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"s_relay_base": HI6_S_RELAY_BASE, **top, "sockets": list(entries)}


def _axis_sockets() -> dict[str, Any]:
    """손으로 옮겨 두었던 holding 25000~25009 를 GET_AXIS_INFO 소켓 두 개(1~3축, 4~6축)로 옮긴 맵."""
    return _sockets(
        {"name": "axis_1_3", "s_block": 2000, "command": "GET_AXIS_INFO", "params": [1, 1]},
        {"name": "axis_4_6", "s_block": 2020, "command": "GET_AXIS_INFO", "params": [1, 4]},
    )


class FakeHi6:
    """holding 레지스터만 흉내 낸다. 쓰기 순서와 읽기 요청을 기록한다."""

    def __init__(self) -> None:
        self.registers: dict[int, int] = {}
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, list[int]]] = []

    def read_holding_registers(self, address: int, count: int = 1) -> list[int]:
        self.reads.append((address, count))
        return [self.registers.get(a, 0) for a in range(address, address + count)]

    def write_register(self, address: int, value: int) -> None:
        self.write_registers(address, [value])

    def write_registers(self, address: int, values: list[int]) -> None:
        self.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.registers[address + i] = v

    def reboot(self) -> None:
        self.registers.clear()


def run_builtin_suite(runner: SmokeRunner) -> None:
    loaded: list[CommandCatalog] = []

    def load() -> str:
        loaded.append(load_catalog(EXAMPLE_CATALOG))
        return ", ".join(f"{c.command_name}({c.command})" for c in loaded[0].commands.values())

    runner.check("load example catalog (real HI6 rows)", load, required=True)
    catalog = loaded[0]

    def point(command: str, name: str) -> CatalogPoint:
        c = catalog.get(command)
        return next(p for p in c.params + c.data if p.name == name)

    def hi6_types() -> str:
        cnd = point("GET_CUR_SPOTGUN_NO", "master_cnd_no")
        bitmap = point("GET_PROFIBUS_DP_STATUS", "status_or_slave_bitmap")
        expect((cnd.size, cnd.offset, cnd.end), (2, 5, 7))
        expect((bitmap.size, bitmap.length, bitmap.end), (16, 16, 20))
        expect(catalog.get("SET_AXIS_INFO").is_control, True)
        return "s2@5 = blocks 5~6, byte[16]@4 = blocks 4~19, SET_AXIS_INFO = control"

    def block_order() -> str:
        # 레지스터 하나 = [.L, .H] = [짝수 block, 홀수 block]
        blocks = registers_to_blocks([0x1234, 0xABCD])
        expect(list(blocks), [0x34, 0x12, 0xCD, 0xAB])
        expect(blocks_to_registers(blocks), [0x1234, 0xABCD])
        return "0x1234 -> blocks [34, 12]"

    def misaligned_value() -> str:
        # master_cnd_no(s2@5) 는 레지스터 2 의 .H 와 레지스터 3 의 .L 에 걸친다
        registers = [2000, 0, 0x2A00 | 0x07, 0x0001, 0, 0, 0, 0, 0, 0]
        blocks = registers_to_blocks(registers)
        expect(decode_point(point("GET_CUR_SPOTGUN_NO", "master_gun_no"), blocks), 7)
        expect(decode_point(point("GET_CUR_SPOTGUN_NO", "master_cnd_no"), blocks), 0x012A)
        return "reg2.H + reg3.L -> 298"

    def matches_word_endian_le() -> str:
        # 정렬된 f4 는 기존 메모리 맵 f32 (word_endian le) 와 같은 값이어야 한다
        raw = struct.pack("<f", 12.5)
        registers = [120, 1, 1, 0, *blocks_to_registers(raw), 0, 0, 0, 0]
        by_block = decode_point(point("GET_AXIS_INFO", "value_0"), registers_to_blocks(registers))
        m = MemoryMap.from_dict({"word_endian": "le", "holding": [{"addr": "4", "type": "f32", "name": "v"}]})
        expect((by_block, m.decode(m.settings.holding[0], registers[4:6])), (12.5, 12.5))
        return "f4@8 == f32 @ register 4 (le) == 12.5"

    def scale_and_format() -> str:
        blocks = bytearray(SOCKET_BLOCKS)
        blocks[2:4] = (253).to_bytes(2, "little")
        temp = decode_point(point("GET_HW_INFO", "cpu_temperature"), bytes(blocks))
        expect(math.isclose(temp, 25.3), True)
        # scale 1 은 적용하지 않아 정수가 그대로 남는다
        blocks[4:6] = (3).to_bytes(2, "little")
        active = decode_point(point("GET_TASK_INFO", "task_active"), bytes(blocks))
        expect((active, type(active)), (3, int))
        blocks[4] = 0b101
        bitmap = decode_point(point("GET_PROFIBUS_DP_STATUS", "status_or_slave_bitmap"), bytes(blocks))
        expect((len(bitmap), bitmap[0]), (16, "0b00000101"))
        return f"cpu_temperature={temp:.1f}, task_active={active}, bitmap[0]={bitmap[0]}"

    def round_trip() -> str:
        checked = 0
        for c in catalog.commands.values():
            for p in c.params + c.data:
                value: Any = [5] * p.length if p.length > 1 else (1.5 if p.catalog_type.is_float else -3 if p.catalog_type.signed else 5)
                if p.format is not None and p.format.value != "dec":
                    value = decode_point(p, bytes(p.offset) + encode_point(p, value) + bytes(SOCKET_BLOCKS))
                blocks = bytes(p.offset) + encode_point(p, value) + bytes(SOCKET_BLOCKS)
                decoded = decode_point(p, blocks)
                expect(decoded if p.scale is None else round(decoded / p.scale), value if p.scale is None else round(value / p.scale))
                checked += 1
        return f"{checked} points encode -> decode"

    def s_relay_address() -> str:
        m = MemoryMap.from_dict(_sockets(
            {"name": "first", "s_block": 0, "command": "GET_TASK_INFO", "params": [0]},
            {"name": "socket", "s_block": 2000, "command": "GET_TASK_INFO", "params": [0]},
            {"name": "next", "s_block": 2020, "command": "GET_TASK_INFO", "params": [0]},
        ), catalog=catalog)
        expect([(s.base, s.end - 1) for s in m.sockets], [(24000, 24009), (25000, 25009), (25010, 25019)])
        expect([s.s_range for s in m.sockets], ["S0000~S0019", "S2000~S2019", "S2020~S2039"])
        return "S0000->24000, S2000->25000, S2020->25010"

    def expand() -> str:
        m = MemoryMap.from_dict(_sockets(
            {"name": "tool", "s_block": 2000, "command": "GET_TOOL_INFO", "params": [3, 2]},
            {"name": "app", "s_block": 2020, "command": "GETSET_TP_APP", "params": [1, 2, 8]},
            {"name": "hw", "s_block": 2040, "command": "GET_HW_INFO"},
        ), catalog=catalog)
        tool, app, hw = m.sockets
        # s1 두 개가 한 레지스터를 나눠 쓴다: .L = tool_no, .H = tool_data_type
        expect(tool.setup_registers, (174, 3 | 2 << 8))
        # param 사이의 data(app_state@6) 자리는 0 으로 둔다
        expect(app.setup_registers, (140, 1, 2, 0, 8))
        expect(app.setup_blocks[6:8], (None, None))
        expect(hw.setup_registers, (170,))
        expect(tool.params, {"tool_no": 3, "tool_data_type": 2})
        return m.describe_plan().replace("\n", " | ")

    def merge() -> str:
        with tempfile.TemporaryDirectory() as d:
            base, sockets = Path(d, "memory_map.json"), Path(d, "sockets.json")
            base.write_text(json.dumps({
                "s_relay_base": HI6_S_RELAY_BASE,
                "holding": [{"addr": "1", "type": "u16", "name": "temperature"}],
            }), encoding="utf-8")
            # Windows 편집기처럼 BOM 을 붙여 둔다
            sockets.write_text(json.dumps({
                "sockets": [{"name": "hw", "s_block": 2000, "command": "GET_HW_INFO"}],
            }), encoding="utf-8-sig")
            m = MemoryMap.from_json(base, sockets, catalog=catalog)
            expect([p.name for p in m.settings.holding], ["temperature"])
            expect([s.base for s in m.sockets], [25000])

            sockets.write_text(json.dumps({"max_gap": 1, "sockets": []}), encoding="utf-8")
            _raises(lambda: MemoryMap.from_json(base, sockets, catalog=catalog), "기준 파일")
        return "memory_map.json + sockets.json (BOM)"

    def no_sockets_unchanged() -> str:
        m = MemoryMap.from_dict({"holding": [{"addr": "25000", "type": "u16", "name": "command", "format": "bin"}]})
        expect((m.sockets, len(m.plan)), ((), 1))
        expect(m.decode(m.settings.holding[0], [5]), "0b0000000000000101")
        expect(m.encode(m.settings.holding[0], "0b101"), [5])
        return m.describe_plan()

    def hi6_profile_matches_manual_map() -> str:
        m = MemoryMap.from_dict(_axis_sockets(), catalog=catalog)
        axis_1_3, axis_4_6 = m.sockets
        # 손으로 옮겨 두었던 holding 25000~25009 (command, params1_type, start_axis, 공란, axis_1~3) 와 같은 번지여야 한다
        expect([axis_1_3.base + p.offset // 2 for p in axis_1_3.points], [25004, 25006, 25008])
        expect(axis_1_3.setup_registers, (120, 1, 1))
        expect(axis_4_6.setup_registers, (120, 1, 4))
        return m.describe_plan().replace("\n", " | ")

    def read_cycle() -> str:
        m = MemoryMap.from_dict(_axis_sockets(), catalog=catalog)
        device = FakeHi6()
        # 이웃한 두 소켓(S2000, S2020)은 한 요청으로 읽는다
        expect([(g[0].base, len(g)) for g in m.socket_plan], [(25000, 2)])

        # 부팅 직후: 소켓이 비어 있어 command 먼저, parameter 나중에 쓰고 이번 값은 E-3101
        sockets = m.read_sockets(device)
        expect(device.reads, [(25000, 20)])
        expect(device.writes, [(25000, [120]), (25001, [1, 1]), (25010, [120]), (25011, [1, 4])])
        expect([(s.error, s.value, s.blocks) for s in sockets], [("E-3101", None, [0] * 20)] * 2)

        # 로봇이 result 를 채운 뒤: 쓰기 없이 소켓마다 result 묶음이 나온다
        device.writes.clear()
        for register, value in ((25004, 10.5), (25016, -2.25)):
            device.registers[register], device.registers[register + 1] = blocks_to_registers(struct.pack("<f", value))
        axis_1_3, axis_4_6 = m.read_sockets(device)
        expect(device.writes, [])
        expect((axis_1_3.command, axis_1_3.command_name, axis_1_3.params), (120, "GET_AXIS_INFO", {"type": 1, "start_axis_no": 1}))
        expect(axis_1_3.value, {"value_0": 10.5, "value_1": 0.0, "value_2": 0.0})
        expect((axis_4_6.params["start_axis_no"], axis_4_6.value["value_1"]), (4, -2.25))
        expect((len(axis_1_3.blocks), axis_1_3.blocks[:6]), (20, [120, 0, 1, 0, 1, 0]))
        expect(axis_1_3.error, None)

        # 재부팅으로 초기화되면 다음 읽기에서 다시 쓴다
        device.reboot()
        sockets = m.read_sockets(device)
        expect(len(device.writes), 4)
        expect({s.error for s in sockets}, {"E-3101"})
        return axis_4_6.model_dump_json()

    def holding_s_notation() -> str:
        m = MemoryMap.from_dict({"s_relay_base": HI6_S_RELAY_BASE, "holding": [
            {"addr": "S0100", "type": "u8", "name": "even_block"},
            {"addr": "S0101", "type": "i8", "name": "odd_block"},
            {"addr": "S0100.3", "type": "bit", "name": "even_bit"},
            {"addr": "s0101.2", "type": "bit", "name": "odd_bit"},
            {"addr": "S0102", "type": "f32", "name": "real"},
        ]})
        expect(
            [(p.register, p.bits) for p in m.settings.holding],
            [(24050, (0, 7)), (24050, (8, 15)), (24050, (3, 3)), (24050, (10, 10)), (24051, None)],
        )
        # addr 는 적은 그대로 Sample 에 실린다
        expect(m.settings.holding[4].addr, "S0102")
        expect([(r.start, r.count) for r in m.plan], [(24050, 3)])
        return "S0100->24050.L, S0101->24050.H, S0100.3->24050.3, S0101.2->24050.A, S0102 f32->24051~24052"

    def variants_share_command() -> str:
        # parameter 값마다 result 배치가 다른 command 는 command_name 을 나눠 같은 번호를 쓴다
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "catalog.csv")
            path.write_text(
                "command,command_name,section,offset,type,name\n"
                "1000,GET_CIFX_STATUS_1,param,2,s1,slot_no\n"
                "1000,GET_CIFX_STATUS_1,param,3,s1,status_group\n"
                "1000,GET_CIFX_STATUS_1,data,4,s4,channel_status\n"
                "1000,GET_CIFX_STATUS_2,param,2,s1,slot_no\n"
                "1000,GET_CIFX_STATUS_2,param,3,s1,status_group\n"
                "1000,GET_CIFX_STATUS_2,data,4,byte[16],reserved\n",
                encoding="utf-8",
            )
            variants = load_catalog(path)
        expect({c.command for c in variants.commands.values()}, {1000})
        return ", ".join(variants.commands)

    def profile_catalogs_load() -> str:
        loaded = []
        for catalog_path in sorted(PROFILES.glob("*/*.csv")):
            profile_catalog = load_catalog(catalog_path)
            sockets = sum(
                len(MemoryMap.from_json(map_path, catalog=profile_catalog).sockets)
                for map_path in catalog_path.parent.glob("*memory_map*.json")
            )
            loaded.append(f"{catalog_path.relative_to(PROFILES)}: {len(profile_catalog.commands)} commands, {sockets} sockets")
        expect(bool(loaded), True)
        return ", ".join(loaded)

    runner.check("HI6 types are byte sized (s2, byte[16])", hi6_types)
    runner.check("register <-> block order (little endian)", block_order)
    runner.check("value across register boundary (s2@5)", misaligned_value)
    runner.check("aligned f4 == memory map f32 word_endian le", matches_word_endian_le)
    runner.check("scale 0.1 / scale 1 keeps int / binary format", scale_and_format)
    runner.check("every example point encode -> decode", round_trip)
    runner.check("S relay block -> Modbus register (S2000 -> 25000)", s_relay_address)
    runner.check("socket expand: s1 params, interleaved param/data", expand)
    runner.check("merge map files (base + sockets)", merge)
    runner.check("map without sockets unchanged (+ bin format)", no_sockets_unchanged)
    runner.check("hi6 profile example = hand-written holding 25000~25009", hi6_profile_matches_manual_map)
    runner.check("read cycle: verify -> rewrite (command first) -> decode", read_cycle)
    runner.check("holding addr in HI6 S notation", holding_s_notation)
    runner.check("variant command_names share one command number", variants_share_command)
    runner.check("profiles/*/ catalogs + examples load", profile_catalogs_load)

    def field_views() -> str:
        # 워드 배열은 절대 주소 포인트로 내려가고, 배열 이름 자체는 샘플이 아니다
        m = MemoryMap.from_dict({
            "word_endian": "le",
            "holding": [
                {"addr": "200", "type": "u16[8]", "name": "position"},
                {"addr": "240", "type": "u16[2]", "name": "servo"},
                {"addr": "3", "type": "u16", "name": "alarm_code", "format": "hex", "scale": 0.1, "offset": 5},
            ],
            "input": [{"addr": "110", "type": "u16[4]", "name": "input_frame"}],
            "fields": [
                {"source": "position", "at": 2, "type": "i32", "names": ["J13_Position", "J14_Position", "J15_Position"]},
                {"source": "servo", "at": 0, "type": "i32", "scale": 0.1, "offset": 1, "names": ["ServoEnabled"]},
                {"source": "input_frame", "at": 0, "type": "f32", "names": ["input_pair"]},
            ],
            "literals": [
                {"name": "Timestamp", "from": "clock", "format": "%Y.%m.%d.%H:%M:%S"},
                {"name": "Model", "from": "const", "value": "igus_DLE-DR-0001"},
            ],
        })
        names = [p.name for p in m.settings.holding]
        expect("position" in names, False)
        expect(
            [n for n in names if n.startswith("J1") or n == "ServoEnabled"],
            ["J13_Position", "J14_Position", "J15_Position", "ServoEnabled"],
        )
        j13 = next(p for p in m.settings.holding if p.name == "J13_Position")
        servo = next(p for p in m.settings.holding if p.name == "ServoEnabled")
        expect((j13.register, j13.point_type, j13.addr), (202, PointType.I32, "202"))
        expect((servo.register, servo.scale, servo.offset), (240, 0.1, 1))
        # le: 앞 워드가 하위. 10 → 10 * 0.1 + 1
        expect(m.decode(servo, [10, 0]), 2.0)
        expect(m.encode(servo, 2.0), [10, 0])
        alarm = next(p for p in m.settings.holding if p.name == "alarm_code")
        expect(m.decode(alarm, [0x10]), "0x0010")
        pair = m.settings.input[0]
        expect((pair.name, pair.register, pair.point_type), ("input_pair", 110, PointType.F32))
        stamped = 1_700_000_000_000
        literals = m.literal_samples(stamped)
        expect([(s.name, s.kind, s.addr, s.type, s.value) for s in literals], [
            ("Timestamp", RegisterKind.LITERAL, "clock:Timestamp", PointType.STR,
             time.strftime("%Y.%m.%d.%H:%M:%S", time.localtime(stamped / 1000))),
            ("Model", RegisterKind.LITERAL, "const:Model", PointType.STR, "igus_DLE-DR-0001"),
        ])
        expect("literal Timestamp" in m.describe_plan(), True)
        expect("J13_Position" in m.describe_plan(), True)
        gallery = MemoryMap.from_json(ROOT / "memory_map.example.json", catalog=catalog)
        gallery_names = {p.name for p in gallery.settings.holding}
        expect("position" in gallery_names, False)
        expect(next(p for p in gallery.settings.holding if p.name == "J13_Position").register, 202)
        expect(next(p for p in gallery.settings.holding if p.name == "s_lo").register, 24100)
        expect(gallery.settings.input[0].name, "battery_voltage")
        expect(next(p for p in gallery.settings.input if p.name == "input_pair").register, 110)
        expect(len(gallery.sockets), 1)
        expect(gallery.literal_samples(0)[1].value, "igus_DLE-DR-0001")
        return "J13_Position@202, ServoEnabled 10 → 2.0, example map"

    runner.check("fields lower to absolute points; literals stay off the read", field_views)

    task = {"name": "a", "s_block": 2000, "command": "GET_TASK_INFO", "params": [0]}
    rejects: list[tuple[str, Callable[[], Any], str]] = [
        ("sockets without s_relay_base",
         lambda: MemoryMap.from_dict({"sockets": [task]}, catalog=catalog), "s_relay_base"),
        ("sockets without catalog",
         lambda: MemoryMap.from_dict(_sockets(task)), "카탈로그"),
        ("odd s_block", lambda: MemoryMap.from_dict(_sockets({**task, "s_block": 2001}), catalog=catalog), "짝수"),
        ("overlapping sockets",
         lambda: MemoryMap.from_dict(_sockets(task, {**task, "name": "b", "s_block": 2010}), catalog=catalog), "겹친다"),
        ("unknown command",
         lambda: MemoryMap.from_dict(_sockets({**task, "command": "NOPE"}), catalog=catalog), "NOPE"),
        ("parameter count",
         lambda: MemoryMap.from_dict(_sockets({**task, "params": []}), catalog=catalog), "1개가 필요"),
        ("parameter out of s2 range",
         lambda: MemoryMap.from_dict(_sockets({**task, "params": [70000]}), catalog=catalog), "범위"),
        ("fraction to integer parameter",
         lambda: MemoryMap.from_dict(_sockets({**task, "params": [1.5]}), catalog=catalog), "소수"),
        ("control (SET) command in socket",
         lambda: MemoryMap.from_dict(_sockets({**task, "command": "SET_AXIS_INFO", "params": [8, 1, 0, 0, 0]}),
                                     catalog=catalog), "제어 command"),
        ("S notation without s_relay_base",
         lambda: MemoryMap.from_dict({"holding": [{"addr": "S0100", "type": "u8", "name": "a"}]}), "s_relay_base"),
        ("S notation u16 on odd block",
         lambda: MemoryMap.from_dict(_sockets(holding=[{"addr": "S0101", "type": "u16", "name": "a"}])), "짝수 S"),
        ("S notation bit without bit index",
         lambda: MemoryMap.from_dict(_sockets(holding=[{"addr": "S0100", "type": "bit", "name": "a"}])), "S0100.3"),
        ("S notation in input",
         lambda: MemoryMap.from_dict(_sockets(input=[{"addr": "S0100", "type": "u8", "name": "a"}])), "input"),
        ("field past source",
         lambda: MemoryMap.from_dict({
             "holding": [{"addr": "200", "type": "u16[8]", "name": "position"}],
             "fields": [{"source": "position", "at": 2, "type": "i32", "names": ["a", "b", "c", "d"]}],
         }), "넘"),
        ("field source missing",
         lambda: MemoryMap.from_dict({
             "fields": [{"source": "nope", "at": 0, "type": "i32", "names": ["a"]}],
         }), "source 없음"),
        ("field source not a word array",
         lambda: MemoryMap.from_dict({
             "holding": [{"addr": "0", "type": "u32", "name": "wide"}],
             "fields": [{"source": "wide", "at": 0, "type": "i32", "names": ["a"]}],
         }), "u16 또는 i16"),
        ("field type is an array",
         lambda: MemoryMap.from_dict({
             "holding": [{"addr": "0", "type": "u16[6]", "name": "raw"}],
             "fields": [{"source": "raw", "at": 0, "type": "i32[3]", "names": ["a"]}],
         }), "names"),
        ("clock literal without format",
         lambda: MemoryMap.from_dict({"literals": [{"name": "Timestamp", "from": "clock"}]}), "format"),
        ("const literal without value",
         lambda: MemoryMap.from_dict({"literals": [{"name": "Model", "from": "const"}]}), "value"),
        ("field name duplicates a point",
         lambda: MemoryMap.from_dict({
             "holding": [
                 {"addr": "0", "type": "u16", "name": "J13_Position"},
                 {"addr": "10", "type": "u16[2]", "name": "raw"},
             ],
             "fields": [{"source": "raw", "at": 0, "type": "u16", "names": ["J13_Position"]}],
         }), "중복"),
        ("non HI6 type", lambda: _command(params=[(2, "u16")], data=[(4, "s2")]), "알 수 없는 type"),
        ("offset in command slot", lambda: _command(params=[(1, "s2")], data=[(4, "s2")]), "command 자리"),
        ("value past 20 blocks", lambda: _command(params=[(2, "s2")], data=[(18, "f4")]), "20 block"),
        ("overlapping parameters", lambda: _command(params=[(2, "s2"), (3, "s1")], data=[(4, "s2")]), "겹친다"),
    ]
    for name, fn, message in rejects:
        runner.check(f"reject: {name}", lambda fn=fn, message=message: _raises(fn, message))


def run_files(runner: SmokeRunner, catalog_path: Path | None, map_paths: list[Path]) -> None:
    catalogs: list[CommandCatalog] = []

    def load_catalog_file() -> str:
        catalogs.append(load_catalog(catalog_path))
        commands = catalogs[0].commands.values()
        control = sum(c.is_control for c in commands)
        return f"{len(commands)} commands ({control} control)"

    if catalog_path is not None:
        runner.check(f"load catalog {catalog_path.name}", load_catalog_file, required=True)

    def load_map() -> str:
        return "\n" + MemoryMap.from_json(*map_paths, catalog=catalogs[0] if catalogs else None).describe_plan()

    runner.check(f"load map {', '.join(p.name for p in map_paths)}", load_map, required=True)


def _command(params: list[tuple[int, str]], data: list[tuple[int, str]]) -> CommandDef:
    def points(prefix: str, rows: list[tuple[int, str]]) -> tuple[CatalogPoint, ...]:
        return tuple(CatalogPoint(name=f"{prefix}{i}", offset=o, type=t) for i, (o, t) in enumerate(rows))

    return CommandDef(command=1, command_name="TEST", params=points("p", params), data=points("d", data))


def _raises(fn: Callable[[], Any], message: str) -> str:
    try:
        fn()
    except (ValueError, ValidationError) as e:
        if isinstance(e, ValidationError):
            text = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        else:
            text = str(e)
        if message not in text:
            raise AssertionError(f"expected {message!r} in error, got: {text}") from e
        return text
    raise AssertionError("not rejected")


def main() -> int:
    parser = argparse.ArgumentParser(description="socket catalog / memory map smoke test (no device)")
    parser.add_argument("--catalog", type=Path, help="command catalog CSV")
    parser.add_argument("--map", type=Path, nargs="+", help="memory map JSON files (first is the base file)")
    args = parser.parse_args()
    if args.catalog and not args.map:
        parser.error("--catalog requires --map")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    runner = SmokeRunner("socket map" + (" files" if args.map else " built-in"))
    try:
        if args.map:
            run_files(runner, args.catalog, args.map)
        else:
            run_builtin_suite(runner)
    except SmokeAbort:
        pass
    return runner.finish()


if __name__ == "__main__":
    # 콘솔 코드페이지가 cp949 여도 한글 메시지가 깨지지 않게 한다
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
