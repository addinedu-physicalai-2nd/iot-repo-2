"""
network/serialModule.py — USB 로 직결된 제어 보드와 Serial 통신 (스레드 기반)

핵심 원칙:
  - 수신 스레드는 orders 를 건드리지 않는다. 센서 이벤트를 큐에 넣기만.
  - 어느 명령을 이 보드로 보낼지(라우팅)는 NetworkManager 가 정한다.
    여기는 '이 포트로 한 줄 주고받기' 만 한다.
  - 포트가 없거나 끊기면(케이블 뽑힘 등) 죽지 않고 계속 재연결을 시도한다.
    WiFi 보드(BoardHub)는 소켓이 새로 붙는 걸로 재연결이 자연스러운데,
    USB 는 같은 포트를 계속 재오픈해줘야 하기 때문이다.

보드 이름을 갖는다. WiFi 보드(BoardHub)와 같은 형식으로 큐에 넣기 위해서다:
  inQueue.put(("board", boardName, msg))
그래서 CentralControl 은 보드가 USB 인지 WiFi 인지 몰라도 된다.

★ PickUpControlBoard.ino 는 JSON 이 아니라 바이너리 프로토콜을 쓴다
  (2바이트 태그 + 고정 길이 payload + '\n'). 여기서 dict 로 바꿔서
  큐에 넣어주기 때문에, mainService.py 는 그대로 dict 만 다루면 된다.

수신 (보드 → 서버, 알림):
  HI + boardId(1B)                        연결 알림   → {"hello": boardName}
  SL + slot(1B) + occupied(1B, 0/1)       슬롯 상태   → {"event": "slotState", "slot": N, "occupied": bool}

수신 (보드 → 서버, RFID 명령 응답):
  GS + status(1B) [+ uid(4B)]             카드 UID 조회 응답
  GT + status(1B) + total(4B)             잔액 조회 응답
  ST + status(1B)                         잔액 기록 응답
  → {"event": "rfidResponse", "cmd": "GS"/"GT"/"ST", "status": int, "ok": bool, ...}
  status 값: 0=OK, 0xFA=NO_TAG, 0xFB=INVALID_TAG, 0xFE=크기부족/모르는명령,
             그외 1~7·0xFF 은 MFRC522::StatusCode 원본 그대로.

송신 (서버 → 보드, NetworkManager 가 cmd 로 라우팅):
  {"cmd": "getCardStatus"}                              → GS
  {"cmd": "getCardBalance", "uid": "<hex8>"}             → GT + uid(4B)
  {"cmd": "setCardBalance", "uid": "<hex8>", "total": N} → ST + uid(4B) + total(4B, little-endian)
  uid 는 hex 문자열(예: "a1b2c3d4")로 주고받는다 — bytes 는 JSON으로 못 보내서.
  ★ total 의 4바이트 엔디안은 보드 쪽이 그냥 raw memcpy 라 서버가 정하기 나름이다.
    지금은 little-endian 으로 골랐다 — 보드 쪽 실제 잔액 계산 코드와 맞는지
    확인 필요.
"""

import threading
import queue
import time

try:
    import serial  # pyserial
except ImportError:
    serial = None  # 아직 설치 전이어도 import 에러 안 나게

RECONNECT_INTERVAL = 1.0  # 포트가 없거나 끊겼을 때 재시도 주기(초)

# RFID 상태 코드 (PickUpControlBoard.ino 와 맞춰야 함)
RFID_STATUS_OK = 0x00
RFID_STATUS_NO_TAG = 0xFA
RFID_STATUS_INVALID_TAG = 0xFB
RFID_STATUS_BAD_REQUEST = 0xFE


class SerialHandler:
    def __init__(self, inQueue: queue.Queue, boardName: str = "board",
                 port: str = "/dev/ttyUSB1", baud: int = 115200):
        self.inQueue = inQueue
        self.boardName = boardName
        self.port = port
        self.baud = baud
        self._ser = None
        self._running = False
        self._writeLock = threading.Lock()  # 송신 직렬화(포트 보호, orders와 무관)

    def start(self):
        if serial is None:
            print(f"[Serial:{self.boardName}] pyserial 미설치 — 더미 모드")
            return
        self._running = True
        threading.Thread(target=self._recvLoop, daemon=True).start()

    # ── 포트 열기 (최초 연결 + 재연결 공용) ────────────────────────
    def _tryOpen(self) -> bool:
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
            print(f"[Serial:{self.boardName}] 연결됨: {self.port} @ {self.baud}")
            return True
        except Exception as e:
            self._ser = None
            print(f"[Serial:{self.boardName}] 포트 열기 실패({self.port}): {e} "
                  f"— {RECONNECT_INTERVAL:.0f}초 뒤 재시도")
            return False

    # ── 수신 루프: 센서 이벤트를 큐에 넣기만 ─────────────────────
    def _recvLoop(self):
        while self._running:
            if self._ser is None:
                if not self._tryOpen():
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            try:
                raw = self._ser.readline()  # '\n' 까지 읽음(블로킹, timeout 있음)
            except Exception as e:
                print(f"[Serial:{self.boardName}] 연결 끊김({e}) — 재연결 시도")
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(RECONNECT_INTERVAL)
                continue

            if not raw:
                continue
            raw = raw.rstrip(b"\r\n")
            if len(raw) < 2:
                continue

            msg = self._decode(raw)
            if msg is not None:
                # ★ orders 안 건드림. 큐로 넘김
                self.inQueue.put(("board", self.boardName, msg))

    # ── 바이너리 프레임 → dict ───────────────────────────────────
    def _decode(self, raw: bytes) -> dict | None:
        tag = raw[:2]

        if tag == b"HI":
            return {"hello": self.boardName}

        if tag == b"SL":
            if len(raw) < 4:
                print(f"[Serial:{self.boardName}] SL 프레임 길이 부족: {raw!r}")
                return None
            return {"event": "slotState", "slot": raw[2], "occupied": bool(raw[3])}

        if tag in (b"GS", b"GT", b"ST"):
            return self._decodeRfid(tag, raw)

        print(f"[Serial:{self.boardName}] 모르는 프레임: {raw!r}")
        return None

    def _decodeRfid(self, tag: bytes, raw: bytes) -> dict:
        status = raw[2] if len(raw) >= 3 else None
        result = {
            "event": "rfidResponse",
            "cmd": tag.decode("ascii"),
            "status": status,
            "ok": status == RFID_STATUS_OK,
        }
        if tag == b"GS" and status == RFID_STATUS_OK and len(raw) >= 7:
            result["uid"] = raw[3:7].hex()
        elif tag == b"GT" and status == RFID_STATUS_OK and len(raw) >= 7:
            result["total"] = int.from_bytes(raw[3:7], "little")
        return result

    # ── dict → 바이너리 프레임 (NetworkManager 가 라우팅해서 호출) ─
    def _encode(self, obj: dict) -> bytes:
        cmd = obj.get("cmd")
        if cmd == "getCardStatus":
            return b"GS\n"
        if cmd == "getCardBalance":
            uid = bytes.fromhex(obj["uid"])
            return b"GT" + uid + b"\n"
        if cmd == "setCardBalance":
            uid = bytes.fromhex(obj["uid"])
            total = int(obj["total"]).to_bytes(4, "little")
            return b"ST" + uid + total + b"\n"
        raise ValueError(f"pickup 보드가 모르는 cmd: {cmd}")

    def send(self, obj: dict) -> bool:
        if self._ser is None:
            print(f"[Serial:{self.boardName}] (더미) 송신: {obj}")
            return False
        try:
            line = self._encode(obj)
        except (KeyError, ValueError) as e:
            print(f"[Serial:{self.boardName}] 잘못된 명령({e}): {obj}")
            return False
        try:
            with self._writeLock:
                self._ser.write(line)
            return True
        except Exception as e:
            print(f"[Serial:{self.boardName}] 송신 실패({e}) — 연결 끊긴 것으로 처리")
            self._ser = None
            return False

    def isOpen(self) -> bool:
        return self._ser is not None

    def stop(self):
        self._running = False
        if self._ser:
            self._ser.close()
