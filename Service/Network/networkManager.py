"""
network/networkManager.py — 통신 스레드 통합 관리

이 클래스가 '네트워크 경계' 다. 바깥(CentralControl)은 통신 방식을 몰라도 된다.

  모듈 구성 (전송 방식별로 파일이 나뉜다)
    TCPModule     :9000 Qt 클라이언트 + :9002 WiFi 보드
    UDPModule     :6000/:6001 카메라 수신 + Admin GUI 로 UDP 영상 송출
    serialModule  USB 직결 보드

  들어오는 것
    - TCP(:9000)   Qt 클라이언트 요청 → inQueue ("tcp", clientId, msg)
    - 보드          USB 든 WiFi 든 똑같이 → inQueue ("board", boardName, msg)
    - CAM(UDP)      프레임은 큐에 안 넣는다. 여기서 최신 1장만 들고 있는다.
                    큐에 넣으면 주문 처리와 같은 줄에 서서 영상이 계속 밀린다.

  나가는 것
    - sendTo / broadcastTcp             : Qt 클라이언트
    - startOrder                          : 그 명령을 맡은 '보드' 로 라우팅
    - 영상은 FrameSender 가 getFrame() 으로 꺼내 UDP 로 쏜다(구독한 곳에만).

★ 보드 구성은 BOARDS 한 곳에서만 정한다.
  ESP 보드 2대 중 하나는 노트북에 USB 직결(serial), 하나는 외부전원 + WiFi(tcp) 다.
  어느 보드가 무슨 역할을 맡을지 아직 미정이라, 역할을 코드가 아니라 표로 뒀다.
  배선이 바뀌면 이 표의 transport/commands 만 고치면 되고
  CentralControl 은 한 줄도 안 바뀐다.
"""

import queue
import time

from Network.TCPModule import QtServer, BoardHub
from Network.UDPModule import CamReceiver, FrameSender
from Network.serialModule import SerialHandler


# 보드 이름 -> 어떻게 붙었고(transport) 어떤 명령을 맡는가(commands)
#   transport: "serial" = USB 직결 / "tcp" = WiFi 로 BoardHub 에 접속
#   commands : 이 보드로 보낼 cmd 이름들 (겹치면 안 됨)
BOARDS: dict[str, dict] = {
    # 분배 보드 — 외부 전원 + WiFi. 상품 배출 모터, 슬롯 분배 모터, 출구 개수 센서.
    # 서버가 startOrder 하나만 보내면 배출~분배~카운트까지 스스로 하고 결과를 보고한다.
    "dispenser": {
        "transport": "tcp",
        "commands": ["startOrder"],
    },
    # 픽업 보드 — 노트북에 USB 직결. 픽업박스 3개 상태 감지(센서 전용).
    "pickup": {
        "transport": "serial",
        "port": "/dev/ttyUSB0",
        "baud": 115200,
        "commands": [],
    },
}


class NetworkManager:
    def __init__(self, inQueue: queue.Queue,
                 tcpPort: int = 9000,
                 boardPort: int = 9002,
                 camPorts: dict[str, int] | None = None,
                 boards: dict[str, dict] | None = None):
        self.inQueue = inQueue
        self.boards = boards if boards is not None else BOARDS

        self.tcp = QtServer(inQueue, port=tcpPort,
                            onClientGone=self.dropCamSubscriptions)

        # ── 보드: USB 는 SerialHandler, WiFi 는 BoardHub 로 ──────
        self.boardHub = BoardHub(inQueue, port=boardPort)
        self.serialBoards: dict[str, SerialHandler] = {
            name: SerialHandler(inQueue, boardName=name,
                                port=cfg.get("port", "/dev/ttyUSB0"),
                                baud=cfg.get("baud", 115200))
            for name, cfg in self.boards.items()
            if cfg["transport"] == "serial"
        }

        # cmd -> boardName 라우팅 표를 BOARDS 에서 만든다
        self._route: dict[str, str] = {}
        for name, cfg in self.boards.items():
            for cmd in cfg.get("commands", []):
                if cmd in self._route:
                    raise ValueError(
                        f"명령 '{cmd}' 가 보드 두 곳에 중복 배정됨: "
                        f"{self._route[cmd]}, {name}")
                self._route[cmd] = name

        # camId -> (seq, jpeg). CAM 스레드가 쓰고 FrameServer 스레드가 읽는다.
        # 키 하나를 쓰는 스레드가 정확히 하나(카메라 1대=스레드 1개)이고
        # 항목을 통째로 교체하므로 경쟁이 없다 → Lock 불필요.
        self._frames: dict[str, tuple[int, bytes]] = {}
        camPorts = camPorts or {}
        self.cams = [CamReceiver(camId, port, onFrame=self._onFrame)
                     for camId, port in camPorts.items()]

        # 영상은 UDP 로 내보낸다. 받을 쪽이 제어 채널로 구독을 신청한다.
        self.frameSender = FrameSender(self.getFrame)

    # ── 전부 시작 ────────────────────────────────────────────────
    def startAll(self):
        self.tcp.start()
        self.boardHub.start()
        for sb in self.serialBoards.values():
            sb.start()
        self.frameSender.start()
        for cam in self.cams:
            cam.start()

    def stopAll(self):
        self.tcp.stop()
        self.boardHub.stop()
        for sb in self.serialBoards.values():
            sb.stop()
        self.frameSender.stop()
        for cam in self.cams:
            cam.stop()

    # ── 보드 명령 라우팅 ─────────────────────────────────────────
    def sendBoard(self, obj: dict) -> bool:
        """cmd 를 맡은 보드로 보낸다. 전송 방식(USB/WiFi)은 여기서 흡수한다."""
        name = self._route.get(obj.get("cmd"))
        if name is None:
            print(f"[NET] '{obj.get('cmd')}' 를 맡은 보드가 없음 — BOARDS 확인")
            return False
        sb = self.serialBoards.get(name)
        ok = sb.send(obj) if sb is not None else self.boardHub.sendToBoard(name, obj)
        self.logComm("toBoard", name, obj, ok)
        return ok

    # ── 통신 로그 중계 ───────────────────────────────────────────
    def logComm(self, direction: str, peer: str, payload: dict, ok: bool = True):
        """오간 메시지를 Qt 관리자 화면으로 중계한다.

        대시보드는 :9000 만 보고 있어서 보드 통신(:9002)을 직접 볼 수 없다.
        서버가 유일한 목격자라 여기서 중계한다.

        commLog 자체는 중계하지 않는다(무한 루프).
        """
        if payload.get("cmd") == "commLog":
            return
        self.tcp.broadcast({
            "cmd": "commLog",
            "dir": direction,          # toBoard / fromBoard / toQt
            "peer": peer,
            "ok": ok,
            "payload": payload,
            "ts": time.time(),
        })

    def boardFor(self, cmd: str) -> str | None:
        """그 명령을 맡은 보드 이름. 역할 배정은 BOARDS 표가 정한다."""
        return self._route.get(cmd)

    def boardStatus(self) -> dict[str, bool]:
        """보드별 접속 여부. WiFi 는 실제 접속, USB 는 포트 열림 기준."""
        online = set(self.boardHub.connectedBoards())
        return {name: (name in online if cfg["transport"] == "tcp"
                       else self.serialBoards[name].isOpen())
                for name, cfg in self.boards.items()}

    # 출고 지시 — CentralControl 은 이 함수 하나만 알면 된다
    def startOrder(self, orderId: int, counts: list[int], slot: int) -> bool:
        """분배 보드에 주문 하나를 통째로 넘긴다.

        counts 는 DISPENSER_PRODUCTS 순서의 개수 3개, slot 은 놓을 픽업박스 번호.
        보드는 배출 → 컨베이어 → 슬롯 분배 → 출구 개수 세기까지 스스로 하고
        orderComplete / orderFailed 로 결과만 보고한다.
        """
        return self.sendBoard({"cmd": "startOrder", "orderId": orderId,
                               "counts": counts, "slot": slot})

    # ── 영상 프레임 ──────────────────────────────────────────────
    def _onFrame(self, camId: str, jpeg: bytes):
        """CAM 수신 스레드에서 호출된다. 최신 1장만 덮어쓴다.

        쌓아두지 않는 게 핵심이다. 모아두면 소비가 밀릴 때 지연이 계속
        누적된다(오래된 화면이 나옴). 덮어쓰면 느린 클라이언트는
        프레임을 건너뛸 뿐 항상 '지금' 을 본다.
        """
        seq = self._frames.get(camId, (0, None))[0]
        self._frames[camId] = (seq + 1, jpeg)
        # TODO: 녹화(SR-26)가 필요해지면 여기서 파일로도 떨군다.

    def getFrame(self, camId: str) -> tuple[int, bytes] | None:
        """최신 프레임 (seq, jpeg). 아직 없으면 None.

        FrameSender 가 자기 주기로 당겨간다. push 가 아니라 pull 인 이유는,
        느린 상대가 CAM 스레드를 붙잡지 못하게 하기 위해서다.
        """
        return self._frames.get(camId)

    # ── 영상 구독 (제어 채널에서 넘어온 요청) ────────────────────
    def watchCam(self, host: str, port: int, camId: str, fps: int | None = None):
        self.frameSender.subscribe(host, port, camId, fps)

    def unwatchCam(self, host: str, port: int, camId: str | None = None):
        self.frameSender.unsubscribe(host, port, camId)

    def dropCamSubscriptions(self, host: str):
        self.frameSender.unsubscribeHost(host)

    def clientAddress(self, clientId: int) -> str | None:
        """그 Qt 클라이언트의 IP. 영상을 어디로 쏠지 정하는 데 쓴다."""
        return self.tcp.clientAddress(clientId)

    def camIds(self) -> list[str]:
        return [cam.camId for cam in self.cams]

    # ── Qt 클라이언트 송신 ───────────────────────────────────────
    def sendTo(self, clientId: int, obj: dict):
        self.tcp.sendTo(clientId, obj)

    def broadcastTcp(self, obj: dict):
        self.tcp.broadcast(obj)
        self.logComm("toQt", "all", obj)
