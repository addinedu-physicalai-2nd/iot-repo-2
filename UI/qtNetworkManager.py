"""
qtNetworkManager.py — Qt 쪽 네트워크 경계

서버의 network/networkManager.py 와 대칭이다.
UI(adminQt / customerQt)는 포트도 소켓도 프로토콜도 모른다.
QtNetworkManager 에 명령을 넣고, 시그널로 결과를 받아 화면만 그린다.

  QtNetworkManager 바깥 창구. 아래 둘을 소유하고 신호를 모아준다
    ServiceClient   TCP :9000  제어 채널 — JSON 한 줄 (요청/응답 + 서버 push)
    ImageReceiver   UDP        영상 채널 — 서버가 쏘는 청크를 재조립

Admin GUI 만 영상을 받는다(TCP+UDP). Customer GUI 는 제어만 쓴다(TCP).

영상 구독은 제어 채널로 신청한다:
    → {"cmd": "watchCam", "camId": "checkout", "udpPort": 7001, "fps": 12}
그 뒤 서버가 그 UDP 포트로 청크를 쏜다. 패킷 포맷은 카메라 것과 같다:
    [frameId(4B)][totalChunks(2B)][chunkIndex(2B)][chunk data]

QTcpSocket / QUdpSocket 이라 별도 스레드가 없다. Qt 이벤트 루프 위에서
처리되므로 받은 값으로 위젯을 바로 건드려도 안전하다.
"""

import json
import struct
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QHostAddress, QTcpSocket, QUdpSocket

RECONNECT_MS = 2000
HEADER = struct.Struct(">IHH")   # frameId, totalChunks, chunkIndex — 서버와 동일
HEADER_SIZE = HEADER.size
FRAME_TIMEOUT = 2.0              # 조각이 덜 온 프레임을 버리는 기준(초)
MAX_PENDING = 8                  # 동시에 들고 있을 미완성 프레임 수


class _ReconnectingSocket(QObject):
    """끊기면 다시 붙는 QTcpSocket 공통 뼈대.

    제어 채널과 영상 채널이 재접속 처리가 똑같아서 여기로 모았다.
    받은 바이트를 어떻게 자를지는 _onBytes 에서 각자 정한다.
    """

    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self, host: str, port: int, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self._stopped = True

        self._sock = QTcpSocket(self)
        self._sock.connected.connect(self._onConnected)
        self._sock.disconnected.connect(self._onDisconnected)
        self._sock.readyRead.connect(self._onReadyRead)
        self._sock.errorOccurred.connect(lambda _: self._scheduleRetry())

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(RECONNECT_MS)
        self._retry.timeout.connect(self._connectNow)

    # ── 시작/정지 ────────────────────────────────────────────────
    def start(self):
        self._stopped = False
        self._connectNow()

    def stop(self):
        self._stopped = True
        self._retry.stop()
        self._sock.abort()
        self._reset()

    def isConnected(self) -> bool:
        return self._sock.state() == QAbstractSocket.SocketState.ConnectedState

    def _connectNow(self):
        if self._stopped:
            return
        if self._sock.state() != QAbstractSocket.SocketState.UnconnectedState:
            return          # 이미 연결됐거나 연결 중
        self._reset()
        self._sock.connectToHost(self.host, self.port)

    def _scheduleRetry(self):
        if self._stopped or self._retry.isActive():
            return
        self._sock.abort()  # 소켓을 Unconnected 로 되돌려 재연결 가능하게
        self._retry.start()

    # ── 소켓 콜백 ────────────────────────────────────────────────
    def _onConnected(self):
        self._retry.stop()
        self._onOpen()
        self.connected.emit()

    def _onDisconnected(self):
        self._reset()
        self.disconnected.emit()
        self._scheduleRetry()

    def _onReadyRead(self):
        self._onBytes(bytes(self._sock.readAll()))

    def _write(self, data: bytes) -> bool:
        if not self.isConnected():
            return False
        self._sock.write(data)
        return True

    # ── 서브클래스가 채운다 ──────────────────────────────────────
    def _onOpen(self):
        """연결 직후 보낼 것이 있으면 여기서."""

    def _reset(self):
        """수신 버퍼 초기화."""

    def _onBytes(self, chunk: bytes):
        raise NotImplementedError


class ServiceClient(_ReconnectingSocket):
    """:9000 제어 채널 — UTF-8 JSON 한 줄 + '\\n'"""

    message = pyqtSignal(dict)

    def __init__(self, host: str, port: int = 9000, parent=None):
        self._buffer = bytearray()
        super().__init__(host, port, parent)

    def _reset(self):
        self._buffer = bytearray()

    def send(self, obj: dict) -> bool:
        """끊긴 상태면 조용히 False. 재연결되면 UI 가 다시 조회한다."""
        return self._write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def _onBytes(self, chunk: bytes):
        # 바이트로 모은다(한글이 패킷 경계에서 쪼개져도 안전)
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, rest = self._buffer.split(b"\n", 1)
            self._buffer = bytearray(rest)
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"[QtNetworkManager] 파싱 실패: {e}")
                continue
            if isinstance(msg, dict):
                self.message.emit(msg)


class ImageReceiver(QObject):
    """UDP 영상 수신 — 카메라 하나를 맡는다.

    서버가 쏘는 청크를 모아 한 장으로 맞춘다. 조각이 하나라도 빠지면
    그 프레임은 버린다(무선이라 종종 빠진다). 늦게 오는 조각 때문에
    메모리가 늘지 않도록 FRAME_TIMEOUT 이 지난 미완성 프레임은 폐기한다.
    """

    frame = pyqtSignal(bytes)

    def __init__(self, camId: str, fps: int = 12, parent=None):
        super().__init__(parent)
        self.camId = camId
        self.fps = fps
        self._sock = QUdpSocket(self)
        self._sock.readyRead.connect(self._onReadyRead)
        self._pending: dict[int, dict] = {}
        self._statDone = 0
        self._statLost = 0

    def bindAny(self) -> int:
        """빈 UDP 포트를 잡고 그 번호를 돌려준다. 서버에 알려줄 값이다."""
        if self._sock.state() == QAbstractSocket.SocketState.BoundState:
            return self._sock.localPort()
        self._sock.bind(QHostAddress.SpecialAddress.AnyIPv4, 0)
        return self._sock.localPort()

    def port(self) -> int:
        return self._sock.localPort()

    def close(self):
        self._sock.close()
        self._pending.clear()

    def stats(self) -> tuple[int, int]:
        """(완성, 유실) 을 돌려주고 카운터를 비운다."""
        done, lost = self._statDone, self._statLost
        self._statDone = self._statLost = 0
        return done, lost

    # ── 청크 재조립 ──────────────────────────────────────────────
    def _onReadyRead(self):
        while self._sock.hasPendingDatagrams():
            datagram = self._sock.receiveDatagram()
            self._handlePacket(bytes(datagram.data()))
        self._dropStale()

    def _handlePacket(self, data: bytes):
        if len(data) <= HEADER_SIZE:
            return
        frameId, total, index = HEADER.unpack_from(data, 0)
        if total == 0 or index >= total:
            return

        entry = self._pending.get(frameId)
        if entry is None:
            if len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending, key=lambda f: self._pending[f]["t"])
                del self._pending[oldest]
                self._statLost += 1
            entry = {"chunks": {}, "total": total, "t": time.monotonic()}
            self._pending[frameId] = entry
        elif entry["total"] != total:
            return                        # 같은 frameId 인데 조각 수가 다르다

        entry["chunks"][index] = data[HEADER_SIZE:]
        if len(entry["chunks"]) == total:
            del self._pending[frameId]
            jpeg = b"".join(entry["chunks"][i] for i in range(total))
            if jpeg[:2] != b"\xff\xd8":   # 합쳤는데 JPEG 가 아니다
                return
            self._statDone += 1
            self.frame.emit(jpeg)

    def _dropStale(self):
        now = time.monotonic()
        stale = [fid for fid, e in self._pending.items()
                 if now - e["t"] > FRAME_TIMEOUT]
        for fid in stale:
            del self._pending[fid]
        self._statLost += len(stale)


class QtNetworkManager(QObject):
    """UI 가 쓰는 창구. 포트와 소켓을 여기서 감춘다."""

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    message = pyqtSignal(dict)
    frame = pyqtSignal(str, bytes)        # camId, jpeg
    cameraState = pyqtSignal(str, bool)   # camId, 연결됨

    def __init__(self, host: str = "127.0.0.1", port: int = 9000, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port

        self._control = ServiceClient(host, port, parent=self)
        self._control.connected.connect(self.connected)
        self._control.disconnected.connect(self.disconnected)
        self._control.message.connect(self.message)

        self._cameras: dict[str, ImageReceiver] = {}
        # 제어 연결이 끊겼다 붙으면 서버 쪽 구독이 날아가 있으므로 다시 신청한다
        self._control.connected.connect(self._resubscribeCameras)

    # ── 제어 채널 ────────────────────────────────────────────────
    def start(self):
        self._control.start()

    def stop(self):
        self._control.stop()
        for receiver in self._cameras.values():
            receiver.close()

    def isConnected(self) -> bool:
        return self._control.isConnected()

    def send(self, obj: dict) -> bool:
        return self._control.send(obj)

    # ── 영상 채널 ────────────────────────────────────────────────
    def watchCamera(self, camId: str, fps: int = 12):
        """그 카메라 영상을 받기 시작한다. 화면에 보일 때만 부른다.

        UDP 포트를 하나 잡고, 그 번호를 제어 채널로 서버에 알린다.
        """
        receiver = self._cameras.get(camId)
        if receiver is None:
            receiver = ImageReceiver(camId, fps, parent=self)
            receiver.frame.connect(lambda jpeg, c=camId: self.frame.emit(c, jpeg))
            self._cameras[camId] = receiver
        udpPort = receiver.bindAny()
        ok = self.send({"cmd": "watchCam", "camId": camId,
                        "udpPort": udpPort, "fps": fps})
        self.cameraState.emit(camId, ok)

    def unwatchCamera(self, camId: str):
        """안 보는 영상을 계속 받지 않게 구독을 끊는다."""
        receiver = self._cameras.get(camId)
        if receiver is None:
            return
        self.send({"cmd": "unwatchCam", "camId": camId, "udpPort": receiver.port()})
        receiver.close()
        self.cameraState.emit(camId, False)

    def _resubscribeCameras(self):
        for camId, receiver in self._cameras.items():
            if receiver.port():
                self.watchCamera(camId, receiver.fps)

    def isWatching(self, camId: str) -> bool:
        receiver = self._cameras.get(camId)
        return receiver is not None and receiver.port() != 0

    def cameraStats(self, camId: str) -> tuple[int, int]:
        """(완성, 유실) — 무선 구간 상태 확인용."""
        receiver = self._cameras.get(camId)
        return receiver.stats() if receiver else (0, 0)
