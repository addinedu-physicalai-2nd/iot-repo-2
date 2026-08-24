"""
network/serialModule.py — USB 로 직결된 제어 보드와 Serial 통신 (스레드 기반)

핵심 원칙:
  - 수신 스레드는 orders 를 건드리지 않는다. 센서 이벤트를 큐에 넣기만.
  - 어느 명령을 이 보드로 보낼지(라우팅)는 NetworkManager 가 정한다.
    여기는 '이 포트로 JSON 한 줄 주고받기' 만 한다.
  - 포트가 없거나 끊기면(케이블 뽑힘 등) 죽지 않고 계속 재연결을 시도한다.
    WiFi 보드(BoardHub)는 소켓이 새로 붙는 걸로 재연결이 자연스러운데,
    USB 는 같은 포트를 계속 재오픈해줘야 하기 때문이다.

보드 이름을 갖는다. WiFi 보드(BoardHub)와 같은 형식으로 큐에 넣기 위해서다:
  inQueue.put(("board", boardName, msg))
그래서 CentralControl 은 보드가 USB 인지 WiFi 인지 몰라도 된다.

수신 예: 보드가 JSON 한 줄 + '\n' 로 올림
  {"event": "boxStatus", "boxes": [1, 0, 0]}   픽업박스 3개 점유 상태
"""

import json
import threading
import queue
import time

try:
    import serial  # pyserial
except ImportError:
    serial = None  # 아직 설치 전이어도 import 에러 안 나게

RECONNECT_INTERVAL = 1.0  # 포트가 없거나 끊겼을 때 재시도 주기(초)


class SerialHandler:
    def __init__(self, inQueue: queue.Queue, boardName: str = "board",
                 port: str = "/dev/ttyUSB0", baud: int = 115200):
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
        buffer = ""
        while self._running:
            if self._ser is None:
                if not self._tryOpen():
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            try:
                data = self._ser.readline()  # '\n' 까지 읽음(블로킹, timeout 있음)
            except Exception as e:
                print(f"[Serial:{self.boardName}] 연결 끊김({e}) — 재연결 시도")
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                buffer = ""
                time.sleep(RECONNECT_INTERVAL)
                continue

            if not data:
                continue
            buffer += data.decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # ★ orders 안 건드림. 큐로 넘김
                self.inQueue.put(("board", self.boardName, msg))

    # ── 송신 (NetworkManager 가 라우팅해서 호출) ─────────────────
    def send(self, obj: dict) -> bool:
        if self._ser is None:
            print(f"[Serial:{self.boardName}] (더미) 송신: {obj}")
            return False
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
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
