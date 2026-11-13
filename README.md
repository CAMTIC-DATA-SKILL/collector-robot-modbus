# collector_robot_modbus
로봇 데이터 수집을 전담하는 컬렉터 레포지토리로 modbus로 구축함을 전제로 한다

## 아키텍처

```
로봇 Controller ──Modbus(TCP/RTU)──▶ collector_robot_modbus ──IPC(ZeroMQ)──▶ 게이트웨이 프로세스 ──▶ 상위 아키텍처
```

- **남향**: 로봇 Controller 와 Modbus(TCP/RTU)로 연동해 데이터 수집 및 제어 통신을 수행한다.
- **이 레포의 범위**: 수집(메모리 맵 기반 주기 읽기·디코딩)과 수집 데이터를 IPC 로 넘기는 데까지.
- **북향**: 상위 아키텍처로의 전송은 이 레포가 담당하지 않는다. IPC 로 연계된 별도 프로세스가 전달한다.

## 구성

```
main.py                  # 진입점: Modbus 주기 읽기 → ZeroMQ data/health PUB, 게이트웨이 cmd SUB
config.py                # .env 로딩. 환경 변수는 모두 여기서만 읽는다
memory_map.example.json  # 메모리 맵에 적을 수 있는 형식 견본. 실행용 맵은 profiles/
docs/
└── command_catalog.example.csv  # 소켓 command 카탈로그 CSV 형식 예시 (HI6 카탈로그에서 형식별로 골라 둔 행)
profiles/
└── hi6/                 # 로봇 모델별 프로파일
    ├── hi6_s_relay_command_catalog.csv  # HI6 S relay command 카탈로그 (매뉴얼 전사)
    └── hi6_s_relay_memory_map.json      # command_catalog·s_relay_base·sockets 를 쓰는 메모리 맵 예시
core/
├── modbus_client/       # Modbus 클라이언트 (BaseModbusClient ← ModbusTcpClient / ModbusRtuClient), 메모리 맵 읽기, 소켓 block 코덱
└── zeromq_client/       # 게이트웨이 IPC (PUB + SUB 파사드 ZeroMqClient, 토픽 조립)
model/                   # pydantic 모델 (메모리 맵 스키마, command 카탈로그, 읽기 요청/결과, IPC 프로토콜)
script/                  # 스모크 테스트
```

## 환경 준비

Python 3.10 이상 기준이다.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

실행용 맵은 `.env` 의 `MEMORY_MAP_FILE` 이다. 기본은 `profiles/hi6/hi6_s_relay_memory_map.json` 이다.
`memory_map.example.json` 은 포인트·필드·리터럴·소켓을 한 파일에 모아 둔 작성 견본이라 장비에 그대로 넣지 않는다.


## 실행

```bash
python main.py
```

### 서비스 등록 (Linux)

`.venv` 와 `.env` 를 만든 뒤, 프로젝트 루트에서 등록합니다. 유닛은 현재 경로를 기준으로 생성됩니다 (`systemd/*.service` 는 `/opt` 고정 설치 예시).

```bash
sudo bash service_add.sh
journalctl -u collector-robot-modbus -f
```

- `/etc/systemd/system` 에 시스템 유닛을 등록하고 `multi-user.target` 으로 부팅 시 자동 기동합니다.
- 실행 계정은 프로젝트 디렉터리 소유자입니다. `data` 쓰기 권한과 맞춥니다.
- 제거: `sudo systemctl disable --now collector-robot-modbus && sudo rm /etc/systemd/system/collector-robot-modbus.service && sudo systemctl daemon-reload`

`.env` 의 `MODBUS_MODE`(tcp/rtu) 접속 정보로 연결하고, `MEMORY_MAP_FILE` 의 포인트를
`POLL_INTERVAL_SEC` 마다 읽어 ZeroMQ 로 게이트웨이에 보낸다.
연결이 끊기면 `RECONNECT_INTERVAL_SEC` 후 다시 연결한다.
시작 시 실제로 나갈 읽기 요청(묶인 범위)을 먼저 출력한다.

로그는 collector-plc 와 같은 형식이다. PUB 은 송신에 성공한 헤더 JSON 을 `[DATA]` / `[HEALTH]` / `[ACK]` 로,
SUB 은 이 collector 대상 cmd 를 `[SUB CMD_R]` / `[SUB CMD_W]` 로 INFO 에 그대로 남긴다.
포인트 읽기 오류는 따로 찍지 않고 `[DATA]` 의 `error` 필드로 확인한다. 송신 실패는 `[DATA] zmq pub failed: ...` WARNING 이다.

```
2026-10-12 17:52:00 [INFO] memory map memory_map.json, read plan:
holding 0~13 (14 regs): status_code, sensor_mode, temperature, ...
2026-10-12 17:52:00 [INFO] ZeroMQ ready pub=bind:tcp://127.0.0.1:5557 sub=connect:tcp://127.0.0.1:5555
2026-10-12 17:52:00 [INFO] [HEALTH] {"msg_id":"...","gateway_address":"gateway","collector_address":"robot-1","msg_type":"health","msg_body":{"device_key":"robot-1","ipc_ok":true,"device_ok":true,"reason":null,"observed_ms":1760259120000},"timestamp_ms":1760259120000}
2026-10-12 17:52:00 [INFO] [DATA] {"msg_id":"...","gateway_address":"gateway","collector_address":"robot-1","msg_type":"data","msg_body":{"device_key":"robot-1","sample_ms":1760259120000,"samples":[...],"sockets":[...]},"timestamp_ms":1760259120000}
2026-10-12 17:52:05 [INFO] [SUB CMD_R] {"msg_id":"...","gateway_address":"gateway","collector_address":"robot-1","msg_type":"cmd_r","msg_body":{"action":"SET_SCAN",...},"timestamp_ms":1760259125000}
2026-10-12 17:52:05 [INFO] [ACK] {"msg_id":"...","gateway_address":"gateway","collector_address":"robot-1","msg_type":"ack","msg_body":{"ref_msg_id":"...","device_key":"robot-1","action":"SET_SCAN","status":"rejected","code":"E-4102",...},"timestamp_ms":1760259125000}
```

## 게이트웨이 IPC (ZeroMQ)

collector-plc 와 같은 방식이다. 소켓은 PUB + SUB 두 개이고 프레임은 multipart `[topic, ProtocolHeaderDTO JSON]` 이다.

| 방향 | 소켓 | 토픽 | msg_type |
| --- | --- | --- | --- |
| collector → gateway | PUB (`ZMQ_PUB_ENDPOINT`, bind) | `collector.robot.{DEVICE_KEY}.{msg_type}` | `data` / `health` / `ack` |
| gateway → collector | SUB (`ZMQ_SUB_ENDPOINT`, connect) | `middleware.gateway.cmd_` prefix | `cmd_r` / `cmd_w` |

- `data`: 매 주기 읽기 결과. 고정 포인트는 `samples[]`, 소켓은 `sockets[]` 로 나간다 ([data 메시지](#data-메시지) 참고).
- `health`: Modbus 연결 상태가 바뀔 때 (`device_ok`, 실패 시 `reason` 에 `E-1001` 등). 연결 실패 중에는 재시도마다 낸다.
- `ack`: cmd 처리 결과. cmd 는 아직 처리하지 않으므로 `status=rejected`, `code=E-4102`(CMD_UNSUPPORTED_ACTION) 로 응답한다.
  cmd 는 모든 collector 에 같은 토픽으로 오므로 헤더 `collector_address` 가 `DEVICE_KEY` 와 다르면 무시한다.

collector 마다 PUB 을 bind 하므로 같은 edge 에 collector 가 여럿이면 `ZMQ_PUB_ENDPOINT` 포트가 겹치면 안 되고
(기본 5557, collector-plc 는 5556), 게이트웨이는 collector 별 PUB 엔드포인트에 각각 connect 한다.
게이트웨이 PUB(cmd) 은 하나를 bind 하고 모든 collector 가 connect 한다 (기본 5555).

게이트웨이 없이 확인할 때는 게이트웨이 역할을 하는 CLI 로 PUB 메시지를 본다.

```bash
# collector.robot.* 수신 출력
python -m core.zeromq_client

# cmd_r 을 한 번 보내고 ack 까지 확인 (ZMQ_SUB_ENDPOINT 에 bind 하므로 실제 게이트웨이와 같이 띄우지 않는다)
python -m core.zeromq_client --device-key robot-1 --cmd cmd_r --action SET_SCAN
```

환경 변수 목록과 기본값은 `.env.example` 참고. 코드에서는 `from config import settings` 로만 참조한다.

### data 메시지

토픽 `collector.robot.{DEVICE_KEY}.data`, 헤더(`ProtocolHeaderDTO`)의 `msg_body` 가 `MsgDataDTO` 다.

```json
{
  "msg_id": "0b6f6c2e-6a4f-4f57-9d8e-2c1f0f6a7b3d",
  "gateway_address": "gateway",
  "collector_address": "robot-1",
  "msg_type": "data",
  "timestamp_ms": 1760236380000,
  "msg_body": {
    "device_key": "robot-1",
    "sample_ms": 1760236380000,
    "samples": [
      {"name": "temperature",     "kind": "holding", "addr": "1",               "type": "i16", "value": -2.0,  "error": null},
      {"name": "battery_voltage", "kind": "input",   "addr": "100",             "type": "f32", "value": null,  "error": "E-2203"},
      {"name": "Timestamp",       "kind": "literal", "addr": "clock:Timestamp", "type": "str", "value": "2026.10.12.17:52:00", "error": null},
      {"name": "Model",           "kind": "literal", "addr": "const:Model",     "type": "str", "value": "igus_DLE-DR-0001", "error": null}
    ],
    "sockets": [
      {
        "name": "axis_4_6",
        "s_block": 2020,
        "command": 120,
        "command_name": "GET_AXIS_INFO",
        "params": {"type": 1, "start_axis_no": 4},
        "value": {"value_0": 40.0, "value_1": 50.5, "value_2": 60.0},
        "blocks": [120, 0, 1, 0, 4, 0, 0, 0, 0, 0, 32, 66, 0, 0, 74, 66, 0, 0, 112, 66],
        "error": null
      },
      {
        "name": "task_0",
        "s_block": 2040,
        "command": 100,
        "command_name": "GET_TASK_INFO",
        "params": {"task_no": 0},
        "value": null,
        "blocks": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "error": "E-3101"
      }
    ]
  }
}
```

`msg_body`

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `device_key` | str | `DEVICE_KEY` |
| `sample_ms` | int | 읽은 시각 (epoch ms) |
| `samples` | list | 레지스터 포인트(holding → input) 뒤에 `literals`. 레지스터 읽기가 성공한 주기에만 시각·상수를 붙인다 |
| `sockets` | list | 메모리 맵 `sockets` 의 소켓마다 하나. 맵 순서. 소켓이 없으면 `[]` |

`samples[]` (포인트 하나)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `name` | str | 맵의 포인트 이름 |
| `kind` | `"holding"` / `"input"` / `"literal"` | 맵 섹션. `literal` 은 레지스터 읽기가 아니다 |
| `addr` | str | 포인트는 맵의 addr (`1`, `100.8~F`, 필드로 내린 절대 번지). literal 은 `clock:이름` / `const:이름` |
| `type` | str | 포인트는 맵의 type (배열이면 원소 타입). literal 은 `str` |
| `value` | int / float / str / list / null | 디코딩 값. `hex` / `bin` 이면 `"0xFF38"` / `"0b0101"` 문자열, 배열이면 list. 실패하면 `null` |
| `error` | str / null | 실패 시 오류 코드 |

`sockets[]` (소켓 = command 하나의 result 묶음)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `name` | str | 맵의 소켓 이름 |
| `s_block` | int | 소켓 시작 S block (`2020` = `S2020`) |
| `command` | int | 카탈로그 command 번호 |
| `command_name` | str | 카탈로그 command 이름 |
| `params` | object | 카탈로그 param 이름 → 맵에 적은 값. 같은 command 라도 이 값에 따라 `value` 의 의미가 달라진다 (예: `start_axis_no=4` 면 `value_0` 는 4축) |
| `value` | object / null | 카탈로그 data 이름 → 디코딩 값. 값의 모양은 `samples[].value` 와 같다. `error` 가 있으면 `null` |
| `blocks` | list[int] / null | 읽은 20 block 원본(0~255, `blocks[0]` = `S{s_block}`). 카탈로그 전사 대조용. `E-3101` 이면 다시 쓰기 전에 읽은 값, 읽기 실패면 `null` |
| `error` | str / null | 소켓 단위 오류 코드 |

| 오류 코드 | 의미 |
| --- | --- |
| `E-1xxx` | 연결 오류. data 를 내지 않고 `health` 로 알린 뒤 재연결한다 |
| `E-2001` / `E-2002` | 읽기 / 쓰기 실패 (`E-2002` 는 소켓 다시 쓰기를 장비가 거부) |
| `E-2203` | 장비가 주소를 거부 (Illegal Data Address) |
| `E-3101` | 소켓 command·parameter 가 설정과 달라(재부팅 등) 다시 썼다. 다음 주기부터 `value` 가 나온다 |

## 스모크 테스트

모든 명령은 저장소 루트에서 실행한다. 각 점검은 `[PASS]` / `[FAIL]` 로 출력되고
마지막 줄에 `== PASS n/n ==` 또는 `== FAIL k/n: <실패 항목> ==` 요약이 나온다.
하나라도 실패하면 종료 코드 1 을 반환하므로 CI 나 배포 스크립트에서 그대로 쓸 수 있다.
연결 같은 필수 점검이 실패하면 이후 점검은 건너뛴다.

| 스크립트 | 대상 | 장비 없이 실행 |
| --- | --- | --- |
| `script/smoke_modbus_tcp.py` | `ModbusTcpClient` | `--host` 생략 시 내장 시뮬레이터 |
| `script/smoke_modbus_rtu.py` | `ModbusRtuClient` | `--port` 생략 시 가상 시리얼(pty) + 시뮬레이터 |
| `script/smoke_socket_map.py` | command 카탈로그 / 메모리 맵 `sockets` | 항상 (장비 불필요, Windows 가능). 사용법은 [소켓](#소켓-command-를-쓰면-result-를-채워-주는-영역) 참고 |

### Modbus TCP

```bash
# 내장 시뮬레이터 대상 전체 점검 (127.0.0.1:15020)
python script/smoke_modbus_tcp.py

# 실장비: 읽기 전용 점검
python script/smoke_modbus_tcp.py --host 192.168.0.10 --kind holding --address 0 --count 10

# 실장비: 쓰기 → 다시 읽기 → 원래 값 복원까지 점검
python script/smoke_modbus_tcp.py --host 192.168.0.10 --kind holding --address 100 --write

# 실장비: 지정 값을 쓰고 복원하지 않고 유지 (f32 = 레지스터 2개)
python script/smoke_modbus_tcp.py --host 192.168.0.10 --address 100 --type f32 --write --value 1.5 --keep
```

### Modbus RTU

```bash
# 가상 시리얼 라인(pty 브리지)에 시뮬레이터를 띄워 전체 점검 (Linux 전용)
python script/smoke_modbus_rtu.py

# 실장비: 읽기 전용 점검
python script/smoke_modbus_rtu.py --port /dev/ttyUSB0 --baudrate 9600 --parity N --device-id 1 --count 10

# 실장비: coil 쓰기/복원 점검
python script/smoke_modbus_rtu.py --port /dev/ttyUSB0 --kind coil --address 0 --write

# 실장비: 레지스터 100 의 상위 8비트에 0x12 를 쓰고 유지 (하위 8비트는 보존)
python script/smoke_modbus_rtu.py --port /dev/ttyUSB0 --address 100.H --type u8 --write --value 0x12 --keep
```

시리얼 포트 권한이 없으면 `sudo usermod -aG dialout $USER` 후 재로그인한다.

### Modbus 공통 옵션

| 옵션 | 설명 |
| --- | --- |
| `--kind` | `coil` / `discrete` / `holding`(기본) / `input` |
| `--address` | 시작 번지. 0-based 오프셋이다 (40001 → 0). `holding` 은 메모리 맵 `addr` 처럼 `100.5`, `100.8~F`, `100.H` 비트 구간도 가능 |
| `--count` | 읽을 개수 (기본: `holding` 은 `--type` 이 차지하는 레지스터 수, 그 외 1) |
| `--write` | 실장비 모드에서 `--address` 에 값을 써 보고 다시 읽어 확인한 뒤 원래 값으로 복원. `holding`, `coil` 만 가능. `--value` 가 없으면 최하위 비트만 뒤집어 쓴다 |
| `--value` | `--write` 로 쓸 값. 배열/여러 coil 은 쉼표로 구분 (`1,0,1`), `0x..` 는 원시 비트, 음수는 `--value=-1` 처럼 `=` 로 붙인다 |
| `--keep` | 복원하지 않고 쓴 값을 유지. `--write --value` 와 함께만 쓸 수 있고, 결과에 복원용 `--value` 가 출력된다 |
| `--type` | `holding` 값 타입. 메모리 맵 `type` 과 같다 (`u16` 기본, `u8` `i8` `bit` 는 비트 구간, `u32` `i32` `f32` `u64` `i64` `f64`, `i16[3]` 배열) |
| `--word-endian` | 2레지스터 이상 값의 순서. `le`(기본) / `be`. 메모리 맵 `word_endian` 과 같다 |
| `--device-id` | Modbus unit id (slave id) |
| `--timeout`, `--retries` | 요청 타임아웃(초) / 재시도 횟수 |
| `-v`, `--verbose` | pymodbus 디버그 로그(송수신 프레임) 출력 |

TCP 전용: `--host`, `--port` / RTU 전용: `--port`, `--baudrate`, `--parity`(N/E/O), `--stopbits`(1/2), `--local-echo`

옵션을 주지 않은 값은 `.env` 의 `MODBUS_TCP_*` / `MODBUS_RTU_*` 값(`config.py`)을 따른다.

단, 시뮬레이터 모드 여부는 `--host` / `--port` 인자로만 결정된다.

### Modbus 점검 항목

- **시뮬레이터 모드**: 연결, 4종 읽기(coil/discrete/holding/input), 4종 쓰기 후 되읽기 비교,
  메모리 맵 타입별(8/16/32비트, 비트 구간, 배열, hex) 인코딩 → 쓰기 → 디코딩 왕복, 타입 지정 쓰기의 복원/유지,
  범위 밖 읽기/쓰기가 `ADDR_OUT_OF_RANGE`(E-2203)로 매핑되는지, 끊었다 다시 연결 후 읽기,
  연결 불가 대상이 `CONNECT_FAILED`(E-1001, 재연결 대상)로 매핑되는지.
  시뮬레이터 초기값은 coil=False, discrete=True, holding=5, input=7 이고 레지스터 0~99 번지를 가진다.
- **실장비 모드**: 연결, 지정 영역 읽기, (`--write` 시) 쓰기/되읽기/복원(`--keep` 이면 유지), 재연결 후 읽기.

> `--write` 는 실제 로봇 레지스터 값을 잠시 바꾼다. 복원은 `finally` 로 보장하지만
> 명령/트리거용 레지스터에는 쓰지 말고, 영향이 없는 번지로만 사용한다.
> `--keep` 은 복원하지 않으므로 값이 그대로 남는다. 되읽기가 실패해도 복원하지 않는다.
> 비트 구간 쓰기는 레지스터를 읽고 해당 비트만 바꿔 다시 쓰므로, 그 사이 장비가 같은 레지스터를 바꾸면 덮어쓸 수 있다.

## 메모리 맵

주소는 `holding` / `input` 포인트에만 적는다. 붙어 있는 주소는 한 번의 Modbus 읽기로 묶이고, 응답은 포인트의 주소와 `type` 길이만큼 잘라 샘플이 된다.
`memory_map.example.json` 이 지금 적을 수 있는 형식을 한 파일에 모아 둔 견본이다. 비트 구간, 16~64비트, 배열, `scale`/`offset`, `hex`/`bin`, S relay 주소, 워드 배열을 자르는 `fields`, 시각·상수 `literals`, 소켓이 모두 들어 있다. 그 파일의 번지는 형식 예이므로 장비 주소로 쓰지 않는다.

```json
{
  "word_endian": "le",
  "max_gap": 8,
  "format": "dec",
  "holding": [
    {"addr": "0.8~F", "type": "u8",  "name": "status_code"},
    {"addr": "2",     "type": "i16", "name": "temperature", "scale": 0.1, "offset": -40},
    {"addr": "22",    "type": "i16[6]", "name": "joint_torque", "scale": 0.01},
    {"addr": "200",   "type": "u16[8]", "name": "position"}
  ],
  "fields": [
    {"source": "position", "at": 2, "type": "i32", "names": ["J13_Position", "J14_Position", "J15_Position"]}
  ],
  "literals": [
    {"name": "Timestamp", "from": "clock", "format": "%Y.%m.%d.%H:%M:%S"},
    {"name": "Model", "from": "const", "value": "igus_DLE-DR-0001"}
  ]
}
```

| 키 | 설명 |
| --- | --- |
| `addr` | 0-based 레지스터 번호. `100.5` 는 비트 하나, `100.8~F` 는 비트 구간 (0 = LSB, 10~15 는 A~F). `100.L` / `100.H` 는 하위 / 상위 바이트로 `100.0~7` / `100.8~F` 와 같고 섞어 써도 된다. `holding` 은 HI6 S relay 표기(`S0100`, `S0100.3`)도 된다 ([HI6 S relay 주소 규칙](#hi6-s-relay-주소-규칙)) |
| `type` | `u16` `i16` `u32` `i32` `u64` `i64` `f32` `f64` 는 레지스터 단위, `u8` `i8` `bit` 는 비트 구간으로 지정. `i16[6]` 처럼 배열 가능. `str` 은 레지스터 타입이 아니고 [literals](#literals-시각과-상수) 에서만 쓴다 |
| `scale` `offset` | 디코딩 뒤에 `값 × scale + offset`. 둘 중 적지 않은 항은 건너뛴다. `offset` 이 없거나 `0` 이면 더하지 않아 정수가 정수로 남는다. `hex` / `bin` 은 둘 다 적용하지 않는다 |
| `format` | `dec`(기본) / `hex` / `bin`. 최상위에 두면 전체 기본값, 포인트에 두면 그 포인트만. hex / bin 은 원시 비트를 `"0xFF38"` / `"0b00000101"` 처럼 낸다 |
| `word_endian` | 2레지스터 이상 값의 순서. `le`(기본, 앞 레지스터가 하위 워드) / `be`. `i32` 는 워드 둘을 이 순서로 16비트 붙여 부호 있는 정수로 읽는다. collector-plc 와 같은 의미 |
| `max_gap` | 빈 번지가 이 개수 이하면 같은 요청으로 묶는다 (기본 8). 한 요청은 최대 125 레지스터 |
| `max_read` | 한 요청의 레지스터 수 상한 (기본 125). 장비가 더 긴 읽기를 거부하면 이 값으로 나눈다 |
| `fields` | 선택. `u16` / `i16` 배열 포인트를 워드 인덱스로 자른다. 없는 맵은 포인트만 읽는다 |
| `literals` | 선택. 레지스터가 아닌 시각 문자열과 상수 |

장비가 묶은 범위를 `ILLEGAL DATA ADDRESS` 로 거부하면 레지스터를 공유하는 포인트 단위로 쪼개 다시 읽고,
쪼갠 계획을 다음 읽기부터 그대로 쓴다. 그래도 실패한 포인트만 `Sample.error` 에 코드가 남고 나머지 값은 살아 있다.

### fields (워드 배열을 잘라 포인트로)

한 덩어리로 읽은 워드를 다시 필드에 대응할 때는 새 주소 체계를 만들지 않는다. 읽을 범위는 지금처럼 `u16[N]` 또는 `i16[N]` 포인트이고, `fields` 가 그 배열 안의 워드 인덱스를 절대 주소 포인트로 내린다. 로드가 끝나면 읽기·묶음·`word_endian`·`scale` 은 포인트와 같다. `describe_plan()` 에는 내려간 주소만 나온다. 소스로 쓰인 배열 포인트는 샘플로 나가지 않는다.

`at` 은 배열의 워드 인덱스다. `names` 한 칸이 `type` 하나고, `i32` 처럼 2워드 타입은 이름마다 2워드씩 나아간다. `position` 이 holding 200 의 `u16[8]` 이면 `at: 2` 의 첫 `i32` 는 주소 `202` 다. 끝 워드가 배열 길이를 넘거나, 소스가 `u16`/`i16` 배열이 아니거나, `type` 에 `[3]` 처럼 길이를 적으면 로드를 거부한다. 반복은 `names` 에만 적는다. 필드의 `scale` / `offset` / `format` 은 포인트와 같은 키다.

```json
{"source": "position", "at": 2, "type": "i32", "names": ["J13_Position", "J14_Position", "J15_Position"]},
{"source": "speed",    "at": 0, "type": "i32", "scale": 0.1,  "names": ["J13_Speed", "J14_Speed", "J15_Speed"]},
{"source": "torque",   "at": 0, "type": "i32", "scale": 0.01, "names": ["J13_Torque", "J14_Torque", "J15_Torque"]},
{"source": "tcp",      "at": 0, "type": "i32", "scale": 0.001, "names": ["X", "Y", "Z"]},
{"source": "servo",    "at": 0, "type": "i32", "scale": 0.01, "offset": 1, "names": ["ServoEnabled"]}
```

견본의 `servo` 배율 `0.01` 과 더하기 `1` 은 `값 × scale + offset` 자리를 보여 주는 숫자다. 장비 식의 상수가 정해지면 그 칸만 바꾼다. 워드를 따로 더하는 식은 이 두 칸으로 적지 않는다.

S relay 배열을 소스로 쓰면 샘플 `addr` 는 `S0200` 이 아니라 내려간 절대 레지스터 번호다. `s_relay_base` 24000 에서 `S0200` 의 `u16[4]` 를 `at: 0` 부터 `i32` 두 개로 자르면 `24100`, `24102` 다.

### literals (시각과 상수)

`literals` 는 레지스터를 읽지 않는다. holding / input 읽기가 성공한 뒤에, 그 주기의 `sample_ms` 로 만들어 샘플 목록 끝에 붙인다. 시각 형식은 수집 호스트의 로컬 시각이다.

| `from` | 필요한 키 | 샘플 |
| --- | --- | --- |
| `clock` | `format` (`strftime`) | `addr` `clock:이름`, `type` `str`, 값은 `format` 으로 만든 문자열 |
| `const` | `value` (문자열) | `addr` `const:이름`, `type` `str`, 값은 `value` |

없는 필드는 나가지 않는다. Seq, Online 처럼 이 맵에 연결되지 않은 값은 `literals` 에도 `fields` 에도 적지 않는다.

```python
import time

from config import settings
from core.modbus_client import MemoryMap, ModbusTcpClient

memory_map = MemoryMap.from_json(*settings.memory_map_files)
print(memory_map.describe_plan())       # 내려간 주소와 literal 행 (장비 없이 확인 가능)
with ModbusTcpClient(settings.modbus_tcp) as client:
    samples = memory_map.read(client)   # [Sample(name="status_code", value=1, error=None, ...), ...]
    samples += memory_map.literal_samples(int(time.time() * 1000))
    values = MemoryMap.values(samples)  # {"status_code": 1, "temperature": -20.0, ...} (실패 포인트 제외)
```

## 소켓 (command 를 쓰면 result 를 채워 주는 영역)

현대 HI6 컨트롤러의 S relay socket 은 번지에 command 와 parameter 를 써 두면 남은 자리를 result 로 채워 준다.

### HI6 S relay 주소 규칙

HI6 는 S relay 를 레지스터가 아니라 **block(8bit)** 단위로 번호를 매긴다. 2 block 이 Modbus 레지스터 1개(1 word)이고,
S relay 영역은 Modbus holding **24000** 부터 시작한다.

```
Modbus 레지스터 = 24000 + S번호 // 2
S번호가 짝수 -> 하위 바이트 (.L = bit 0~7)
S번호가 홀수 -> 상위 바이트 (.H = bit 8~F)
```

| HI6 표기 | 메모리 맵 addr |
| --- | --- |
| `S0000` | `24000.L` (`24000.0~7`) |
| `S0001` | `24000.H` (`24000.8~F`) |
| `S2000` | `25000.L` |
| `S2019` | `25009.H` |

`holding` 포인트의 `addr` 에는 S 표기를 그대로 적어도 된다. 같은 맵의 `s_relay_base` 로 위 표처럼 바꿔 읽고,
`Sample.addr` 에는 적은 표기(`S0100`)가 그대로 실린다. 대소문자는 가리지 않는다.

| addr | type | 읽는 위치 (`s_relay_base: 24000`) |
| --- | --- | --- |
| `S0100` / `S0101` | `u8` `i8` | `24050.L` / `24050.H` |
| `S0100.3` / `S0101.3` | `bit` | `24050.3` / `24050.B` (block 안의 비트 0~7) |
| `S0100` | `u16` `i16[4]` `f32` 등 레지스터 단위 | `24050` 부터 (S 번호는 짝수여야 한다) |

- `s_relay_base` 가 없거나, `input` 에 S 표기를 쓰면 시작할 때 거부한다 (S relay 는 holding 영역이다).
- 레지스터 단위 type 을 홀수 S 에 두면 거부한다. 레지스터 경계에 걸친 여러 block 값은 아래 [소켓](#소켓-command-를-쓰면-result-를-채워-주는-영역) 의 카탈로그(`s2`, `f4` 등)로 읽는다.
- `bit` 는 `S0100.3` 처럼 비트 번호까지 적는다.

여러 block 값은 S 번호가 작은 block 이 하위 바이트인 little endian 이다. 2 레지스터 이상인 값이 레지스터 경계에
맞으면 메모리 맵 `word_endian: "le"`(앞 레지스터가 하위 word)와 같은 값이 된다.

### 소켓 한 자리

S relay socket 은 **S2000** 부터 시작하고 한 자리가 **20 block = 10 레지스터** 다.
`command` + `params` + `result` 가 모두 이 안에 들어가므로 **parameter 가 많아질수록 result 가 줄어든다**.

```
소켓 한 자리 = 20 block = 10 레지스터 (S2000~S2019 = holding 25000~25009)
block  0  1 | 2  3  4  5 ...                                         19
      command | parameter 와 result 가 카탈로그 offset 대로 놓인다
       2 block | 값이 없는 칸은 0 으로 온다
다음 소켓: S2020 (holding 25010), S2040 (holding 25020), ...
```

- command 는 HI6 에 정의된 데이터 고유 번호이고 block 0~1(레지스터 1개)을 쓴다.
- parameter 와 result 의 위치·크기는 command 마다 다르고 **block 단위**다. 레지스터 경계에 맞지 않아도 된다.
  예) `GET_TOOL_INFO` 는 1 block parameter 두 개(block 2, 3)가 레지스터 하나를 나눠 쓰고,
  `GET_CUR_SPOTGUN_NO` 의 `master_cnd_no`(s2, block 5~6)는 두 레지스터에 걸친다.
- parameter 와 result 가 섞여 있어도 된다. 예) `GETSET_TP_APP` 은 param(2, 4) → result(6) → param(8).
- 수집기는 소켓 10 레지스터를 20 block 바이트열(레지스터마다 `[.L, .H]`)로 펼쳐 offset 대로 읽고 쓴다.

layout 은 로봇 모델마다 정해져 있고 command 수가 많으므로 메모리 맵에 적지 않고 **command 카탈로그 CSV** 에서 가져온다.

### 어디에 무엇을 적나

| 적을 내용 | 파일 | 단위 |
| --- | --- | --- |
| command 번호, parameter 타입, result 항목 배치 | `profiles/<모델>/` 의 카탈로그 CSV (HI6: `hi6_s_relay_command_catalog.csv`) | 로봇 **모델**당 1개. 매뉴얼 전사이므로 git 에 커밋하고 현장마다 고치지 않는다 |
| `word_endian`, `s_relay_base`, `command_catalog`(소켓 쓸 때만), 고정 주소 포인트, 소켓 배치 | `memory_map.json` (원하면 `sockets` 만 `sockets.json` 으로 분리) | 설치한 로봇 **한 대**당 |
| 메모리 맵 파일 경로 | `.env` 의 `MEMORY_MAP_FILE` | 로봇 한 대당 |

HI6 는 `profiles/hi6/` 의 예시에서 시작한다.

```bash
cp profiles/hi6/hi6_s_relay_memory_map.json memory_map.json   # 소켓 배치와 parameter 값을 현장에 맞춘다
# 맵 JSON 의 command_catalog 가 카탈로그 CSV 를 가리킨다
python script/smoke_socket_map.py --map memory_map.json
```

예시는 `GET_AXIS_INFO`(120) 하나를 `type`(축각도, 베이스좌표, 축속도, 부하율, 엔코더 온도 등)과
`start_axis_no`(1 = 1~3축, 4 = 4~6축)만 바꿔 소켓 22개(`S2000`~`S2439`, holding 25000~25219)에 쓴다.
소켓은 20 block 씩 떨어뜨려 배치한다.

| 파일 / 키 | 내용 | 범위 |
| --- | --- | --- |
| command 카탈로그 CSV (`command_catalog` 키) | command 별 parameter 와 result layout | 로봇 **모델** 공통. 매뉴얼 전사이므로 현장마다 고치지 않는다 |
| 메모리 맵 `sockets` (`MEMORY_MAP_FILE`) | 어느 S block 에 어떤 command 를 어떤 parameter 로 쓸지 | 설치 **개체**별 |

카탈로그 CSV 는 행 하나가 parameter 하나 또는 result 포인트 하나다. command 하나가 여러 행을 차지하므로
`command` / `command_name` 은 행마다 반복해서 적는다 (`docs/command_catalog.example.csv` 참고).

```csv
command,command_name,section,offset,type,name,scale,format,unit,description
120,GET_AXIS_INFO,param,2,s2,type,1,decimal,,"1=현재위치(축각도), ..."
120,GET_AXIS_INFO,param,4,s2,start_axis_no,1,decimal,,시작 축 번호 (1~)
120,GET_AXIS_INFO,data,8,f4,value_0,1,decimal,,(시작축 + 0축) 해당값
170,GET_HW_INFO,data,2,s2,cpu_temperature,0.1,decimal,,cpu 온도 * 10
1010,GET_PROFIBUS_DP_STATUS,data,4,byte[16],status_or_slave_bitmap,1,binary,,Node/슬레이브 상태 bitmap
```

| 컬럼 | 설명 |
| --- | --- |
| `command` | command 정수. `0x..` 도 된다. 같은 `command_name` 의 행은 모두 같은 값이어야 한다. parameter 값에 따라 result 배치가 바뀌는 command 는 `command_name` 을 나눠 같은 번호를 쓴다 (예: `GET_CIFX_STATUS_1` ~ `_5` 가 모두 1000) |
| `section` | `param` 또는 `data`(result). `param` 행 순서가 메모리 맵 `params` 값 순서다. `data` 행이 없으면 제어(SET) command 다 |
| `offset` | 소켓 첫 block(command)을 0 으로 센 **block 위치**. 매뉴얼 표의 위치를 그대로 적는다. command 가 0~1 이므로 2 이상 |
| `type` | HI6 매뉴얼 표기. 숫자는 **byte(block) 수**다: `s1` `s2` `s4` `s8`(부호 있음) `u1` `u2` `u4` `u8`(부호 없음) `f4` `f8`(실수) `byte`(원시 바이트). `byte[16]` 처럼 배열 가능 |
| `scale` | 원시 값에 곱할 배율. 읽을 때 `원시 × scale`, parameter 로 쓸 때는 `값 ÷ scale` 을 반올림해 쓴다. 비우거나 `1` 이면 적용하지 않아 정수는 정수로 남는다. hex / bin 이면 적용하지 않는다 |
| `format` | `dec` / `hex` / `bin`. 매뉴얼 표기 `decimal` / `hexadecimal` / `binary` 도 된다 |
| `unit` `description` | 사람이 보는 값. 수집에는 쓰지 않는다 |

메모리 맵 포인트의 `type` 은 bit 수(`u16`, `f32`)이고 카탈로그의 `type` 은 byte 수(`u2`, `f4`)다.
같은 글자라도 뜻이 다르므로 카탈로그에 `u16` 을 적으면 거부한다.

result 는 같은 자리를 다른 타입으로 보는 행이 있을 수 있어 겹쳐도 되지만, parameter 끼리 겹치면 서로 덮어쓰므로 거부한다.
result 에 공란이 있어 `data` 가 남은 자리를 다 쓰지 않는 것은 정상이다.
카탈로그 오류는 첫 번째에서 멈추지 않고 `파일:행` 과 함께 모두 모아 한 번에 알린다.

메모리 맵에는 S relay 위치(`s_block`)와 command 이름, parameter 값만 적는다. `s_relay_base` 는 `S0000` 이 놓인
Modbus 레지스터(HI6 는 24000)이고 `sockets` 를 쓰면 반드시 적는다.

```json
{
  "word_endian": "le",
  "s_relay_base": 24000,
  "command_catalog": "profiles/hi6/hi6_s_relay_command_catalog.csv",
  "sockets": [
    {"name": "axis_1_3", "s_block": 2000, "command": "GET_AXIS_INFO", "params": [1, 1]},
    {"name": "axis_4_6", "s_block": 2020, "command": "GET_AXIS_INFO", "params": [1, 4]},
    {"name": "tool",     "s_block": 2040, "command": "GET_TOOL_INFO", "params": [3, 2]},
    {"name": "hw",       "s_block": 2060, "command": "GET_HW_INFO"}
  ]
}
```

`command_catalog` 는 저장소 루트 기준(또는 절대경로) CSV 경로다. 소켓이 없는 맵(delta, m1509 등)은 키를 생략하거나 `null` 로 둔다.

`s_block` 은 매뉴얼 표기 `S2000` 의 숫자를 그대로 적는다. 짝수여야 하고 소켓끼리 20 이상 떨어져야 한다.
결과는 소켓 이름별 묶음으로 나가므로 같은 command 를 여러 소켓에 써도 섞이지 않는다 ([소켓 수집 동작](#소켓-수집-동작) 참고).
`describe_plan()` 은 S 범위와 Modbus 범위, parameter 값, result 값마다 S 번호를 같이 보여 주므로 매뉴얼과 바로 대조할 수 있다.
`setup` 은 command·parameter 자리를 덮는 레지스터 값이고, parameter 사이에 낀 result 자리는 0 으로 둔다.

```
socket axis_1_3 S2000~S2019 -> holding 25000~25009 GET_AXIS_INFO(=120)(type=1, start_axis_no=1): setup 25000~25002=[120, 1, 1], result: value_0(S2008 f4), value_1(S2012 f4), value_2(S2016 f4)
socket tool S2040~S2059 -> holding 25020~25029 GET_TOOL_INFO(=174)(tool_no=3, tool_data_type=2): setup 25020~25021=[174, 515], result: tool_weight(S2044 f4), ...
```

`tool` 의 `515` 는 1 block parameter 두 개가 한 레지스터에 들어간 값이다 (`tool_no=3` 이 .L, `tool_data_type=2` 가 .H → `3 + 2×256`).

시작 시 다음은 모두 거부한다.

- `sockets` 가 있는데 `s_relay_base` 나 `command_catalog` 가 없음
- 카탈로그에 없는 command, parameter 개수 불일치, parameter 값이 타입 범위 밖이거나 정수 타입에 소수
- result 가 없는 제어(SET) command 를 수집 소켓에 배치
- 홀수 `s_block`, 소켓끼리 번지 겹침, 소켓 이름 중복
- 카탈로그에서 HI6 표기가 아닌 type, command 자리(block 0~1)를 쓰는 offset, 20 block 을 넘는 값,
  parameter 끼리 겹침, 한 command 안의 name 중복

읽기 전용 `holding` 포인트가 소켓 영역 안에 있으면 경고만 낸다.

카탈로그와 맵은 장비 없이 검증할 수 있다 (Windows 에서도 실행된다).

```bash
# 내장 점검: block 코덱·주소 변환·펼치기·병합·거부 규칙 + profiles/*/ 카탈로그 로드 확인
python script/smoke_socket_map.py

# 배포 전 실제 파일 검증 + 펼친 계획 출력 (카탈로그는 맵의 command_catalog 키)
python script/smoke_socket_map.py --map memory_map.json sockets.json
# 필요하면 --catalog 로 카탈로그를 덮어쓴다
```

### 소켓 수집 동작

`sockets` 에 적은 소켓은 `holding` 에 다시 적지 않아도 `read_sockets()` 가 읽는다 (`read()` 는 holding / input 만).
쓰기(command·parameter)와 읽기(result)가 소켓 한 줄로 정해진다.

1. 소켓 10 레지스터를 통째로 읽는다. 붙어 있는 소켓(S2000, S2020, ...)은 한 요청으로 묶는다.
2. command·parameter 자리가 설정값과 같으면 result 를 디코딩해 소켓마다 결과 하나로 낸다.
3. 다르면(재부팅으로 초기화 등) **command 를 먼저, parameter 를 나중에** 다시 쓰고 그 소켓의 이번 결과는 `E-3101` 로 낸다.
   로봇이 result 를 계속 갱신하므로 다음 읽기부터 값이 나온다.

읽을 때마다 command·parameter 자리도 같이 읽으므로 따로 확인 주기를 두지 않는다 (확인 주기 = `POLL_INTERVAL_SEC`).

소켓 결과는 낱개 샘플로 풀지 않고 command 하나를 한 묶음으로 낸다. data 메시지 `msg_body` 에서 `samples`(holding / input)와
나란히 `sockets` 목록으로 나간다. 필드와 오류 코드는 [data 메시지](#data-메시지) 참고.

### 맵 파일 나누기

`MEMORY_MAP_FILE` 에 쉼표로 여러 경로를 주면 순서대로 합친다. 쓰기가 생기는 `sockets` 만 따로 리뷰할 때 쓴다.

```bash
MEMORY_MAP_FILE=memory_map.json,sockets.json
```

`holding` / `input` / `sockets` / `fields` / `literals` 는 이어 붙이고,
`word_endian` / `max_gap` / `max_read` / `format` / `s_relay_base` / `command_catalog` 는 첫 번째(기준) 파일에만 둔다.
뒤 파일에 있으면 어느 쪽이 이겼는지 추적할 수 없으므로 거부한다. 병합 후 검증은 한 번만 돌므로 파일을 나눠도 결과가 같다.
