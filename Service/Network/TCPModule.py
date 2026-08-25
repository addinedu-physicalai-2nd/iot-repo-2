"""
network/TCPModule.py — TCP 수신 모듈

TCP 로 붙는 상대가 둘이라 서버도 둘이다. 포트를 나눈 이유는:
  - broadcast 가 보드에까지 가면 안 된다(보드는 주문 상태를 알 필요가 없다)
  - Qt 는 clientId 로, 보드는 '이름' 으로 지목해 보낸다

  QtServer  :9000  Qt 클라이언트(대시보드/키오스크)  — UTF-8 JSON 한 줄 + '\n'
  BoardHub  :9002  WiFi 제어 보드(분배 보드)        — 바이너리 프레임

★ 전송 형식이 다르다.
  Qt 는 사람이 읽는 JSON 이 편하고(디버깅·통신 로그), 보드는 ArduinoJson
  파싱과 String 조립 비용이 아까워서 고정 길이 바이너리를 쓴다.
  프레임 정의와 인코딩/디코딩은 Library/protocol.py 한 곳에만 있다.

accept 루프는 형식과 무관해서 TcpServerBase 가 공유하고,
줄 자르기(_readLines)는 JSON 을 쓰는 QtServer 만 쓴다.

★ 이 스레드들은 orders 를 절대 건드리지 않는다.
  받은 것은 inQueue 에 넣기만 하고, 처리·응답은 CentralControl 이 맡는다.
    Qt   → ("tcp",   clientId,  msg)
    보드 → ("board", boardName, msg)
"""

import json
import socket
import threading
import queue

from Library.protocol import (
    CMD_HELLO, PAYLOAD_LEN, TAG_SIZE,
    decodeBoardFrame, encodeBoardFrame,
)


def encodeLine(obj: dict) -> bytes:
    # DB에서 온 datetime 등 json 기본으로 못 바꾸는 값은 str()로 대체.
    # (member.createdAt, orders.createdAt/paidAt 가 여기 걸림)
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


class TcpServerBase:
    """accept 루프를 공유하는 뼈대.

    줄 자르기(_readLines)는 JSON 을 쓰는 QtServer 만 쓴다. BoardHub 는
    자기 방식(_readFrames)으로 바이트를 자른다."""

    def __init__(self, host: str = "0.0.0.0", port: int = 0, tag: str = "TCP"):
        self.host = host
        self.port = port
        self.tag = tag
        self._sock: socket.socket | None = None
        self._running = False

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen()
        self._running = True
        threading.Thread(target=self._acceptLoop, daemon=True).start()
        print(f"[{self.tag}] 리슨 시작: {self.host}:{self.port}")

    def _acceptLoop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._onClient,
                             args=(conn, addr), daemon=True).start()

    def _onClient(self, conn: socket.socket, addr):
        raise NotImplementedError

    def _readLines(self, conn: socket.socket):
        """'\n' 경계로 잘라 한 줄씩 내보낸다. 소켓이 닫히면 끝난다."""
        buffer = ""
        while self._running:
            try:
                data = conn.recv(4096)
            except OSError:
                break
            if not data:
                break
            buffer += data.decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    yield line

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()


class QtServer(TcpServerBase):
    """:9000 — Qt 클라이언트(대시보드·키오스크). clientId 로 구분한다."""

    def __init__(self, inQueue: queue.Queue, host: str = "0.0.0.0", port: int = 9000,
                 onClientGone=None):
        super().__init__(host, port, tag="TCP")
        self.inQueue = inQueue
        self.onClientGone = onClientGone     # 연결이 끊길 때 (ip) 로 호출
        self._clients: dict[int, socket.socket] = {}
        self._addrs: dict[int, str] = {}           # clientId -> IP (영상 보낼 주소)
        self._clientsLock = threading.Lock()       # 소켓 목록 보호(orders 와 무관)
        self._nextId = 0

    def _onClient(self, conn: socket.socket, addr):
        with self._clientsLock:
            clientId = self._nextId
            self._nextId += 1
            self._clients[clientId] = conn
            self._addrs[clientId] = addr[0]
        print(f"[TCP] 연결 #{clientId} {addr}")
        try:
            for line in self._readLines(conn):
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self.sendTo(clientId, {"cmd": "error", "reason": "badJson"})
                    continue
                # ★ orders 안 건드림. 큐에 넣기만 → CentralControl 이 처리
                self.inQueue.put(("tcp", clientId, msg))
        finally:
            with self._clientsLock:
                self._clients.pop(clientId, None)
                host = self._addrs.pop(clientId, None)
            try:
                conn.close()
            except OSError:
                pass
            print(f"[TCP] 해제 #{clientId}")
            # 제어 연결이 끊기면 영상 구독도 같이 정리해야 한다.
            # UDP 는 상대가 죽어도 알 수 없어서 여기서 끊어주지 않으면 계속 쏜다.
            if host and self.onClientGone:
                self.onClientGone(host)

    def sendTo(self, clientId: int, obj: dict):
        with self._clientsLock:
            conn = self._clients.get(clientId)
        if conn is None:
            return
        try:
            conn.sendall(encodeLine(obj))
        except OSError:
            pass

    def clientAddress(self, clientId: int) -> str | None:
        with self._clientsLock:
            return self._addrs.get(clientId)

    def broadcast(self, obj: dict):
        line = encodeLine(obj)
        with self._clientsLock:
            conns = list(self._clients.values())
        for conn in conns:
            try:
                conn.sendall(line)
            except OSError:
                pass

    def stop(self):
        super().stop()
        with self._clientsLock:
            conns = list(self._clients.values())
            self._clients.clear()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass


class BoardHub(TcpServerBase):
    """:9002 — WiFi 제어 보드(분배 보드). ★ 바이너리 프레임을 쓴다.

    프레임 정의는 Library/protocol.py 에 있다:
        [ TAG(ASCII 2B) ][ PAYLOAD (태그별 고정 길이) ]
      보드 → 서버 : HL(hello) / OC(orderComplete) / OF(orderFailed) / OR(orderRejected)
      서버 → 보드 : SO(startOrder)

    바깥(NetworkManager·MainService)에는 예전과 똑같이 dict 로 올려준다.
    바이트 ↔ dict 변환은 이 경계에서만 일어난다.

    ★ 이름 신고가 없어졌다. HL 프레임에는 페이로드가 없어서 보드 이름을
      실을 자리가 없다. :9002 에 붙는 보드가 분배 보드 하나뿐이라 뺀 것이고,
      그래서 이 포트에 붙은 상대는 무조건 boardName 으로 등록한다.
      WiFi 보드가 둘 이상이 되면 HL 에 보드 id 1바이트를 넣어야 한다.

    접속/해제는 서버 내부 이벤트로 큐에 올린다(보드가 보내는 게 아니다):
      {"event": "boardConnected"} / {"event": "boardDisconnected"}
    보드가 리셋되면 여기로 다시 오는데, 그때 서버가 물고 있던 주문을 정리해야 한다.
    """

    def __init__(self, inQueue: queue.Queue, host: str = "0.0.0.0", port: int = 9002,
                 boardName: str = "dispenser"):
        super().__init__(host, port, tag="BOARD")
        self.inQueue = inQueue
        self.boardName = boardName
        self._boards: dict[str, socket.socket] = {}   # boardName -> socket
        self._lock = threading.Lock()

    # ── 프레임 자르기 ────────────────────────────────────────────
    def _readFrames(self, conn: socket.socket):
        """[태그 2B][페이로드] 단위로 잘라 하나씩 내보낸다.

        길이 필드가 없다. 태그를 읽으면 뒤에 몇 바이트가 오는지 PAYLOAD_LEN
        으로 알 수 있어서, '2바이트 읽고 → 표에서 길이 찾고 → 그만큼 더 모은다'
        만 반복하면 된다. TCP 는 스트림이라 프레임이 쪼개져 오거나 여러 개가
        붙어 오므로, JSON 쪽의 '\n' 자르기와 같은 자리를 이게 대신한다.
        """
        buffer = bytearray()
        while self._running:
            try:
                data = conn.recv(4096)
            except OSError:
                break
            if not data:
                break
            buffer += data

            while True:
                if len(buffer) < TAG_SIZE:
                    break
                tag = bytes(buffer[:TAG_SIZE])
                size = PAYLOAD_LEN.get(tag)
                if size is None:
                    # 모르는 태그 = 스트림 어긋남. 구분자가 없으니 1바이트씩
                    # 버리며 다시 맞춘다(그대로 두면 영영 못 읽는다).
                    print(f"[BOARD] 알 수 없는 태그 {tag!r} — 1바이트 버리고 재동기화")
                    del buffer[0]
                    continue
                if len(buffer) < TAG_SIZE + size:
                    break                      # 페이로드가 아직 덜 왔다
                payload = bytes(buffer[TAG_SIZE:TAG_SIZE + size])
                del buffer[:TAG_SIZE + size]
                yield tag, payload

    def _onClient(self, conn: socket.socket, addr):
        name = None
        try:
            for tag, payload in self._readFrames(conn):
                if tag == CMD_HELLO:               # 접속 신고
                    name = self.boardName
                    self._register(name, conn, addr)
                    self.inQueue.put(("board", name, {"event": "boardConnected"}))
                    continue
                if name is None:
                    # 신고도 안 하고 보낸 건 버린다(보드가 리셋 후 HL 을
                    # 빠뜨렸을 수 있어 그냥 무시하고 다음 프레임을 기다린다)
                    print(f"[BOARD] 미등록 보드 {addr} 프레임 무시: {tag!r}")
                    continue
                msg = decodeBoardFrame(tag, payload, self.boardName)
                if msg is None:
                    continue
                # ★ orders 안 건드림. 큐에 넣기만 → CentralControl 이 처리
                self.inQueue.put(("board", name, msg))
        finally:
            if name:
                gone = False
                with self._lock:
                    if self._boards.get(name) is conn:
                        del self._boards[name]
                        gone = True
                print(f"[BOARD] 연결 해제: {name} {addr}")
                if gone:      # 소켓이 교체된 게 아니라 진짜 끊긴 경우만
                    self.inQueue.put(("board", name, {"event": "boardDisconnected"}))
            try:
                conn.close()
            except OSError:
                pass

    def _register(self, name: str, conn: socket.socket, addr):
        with self._lock:
            old = self._boards.get(name)
            self._boards[name] = conn
        if old is not None and old is not conn:
            try:                       # 재부팅 후 재접속 — 낡은 소켓 정리
                old.close()
            except OSError:
                pass
        print(f"[BOARD] 등록: {name} {addr}")

    def sendToBoard(self, name: str, obj: dict) -> bool:
        """dict 로 받아 바이너리 프레임으로 내보낸다.

        바깥은 예전처럼 dict 만 넘기면 된다(관리자 통신 로그도 dict 를 본다).
        """
        with self._lock:
            conn = self._boards.get(name)
        if conn is None:
            print(f"[BOARD] '{name}' 미접속 — 명령 버림: {obj}")
            return False
        try:
            frame = encodeBoardFrame(obj)
        except (ValueError, KeyError, TypeError) as e:
            print(f"[BOARD] 프레임 인코딩 실패 ({e}) — 명령 버림: {obj}")
            return False
        try:
            conn.sendall(frame)
            return True
        except OSError:
            return False

    def connectedBoards(self) -> list[str]:
        with self._lock:
            return sorted(self._boards)

    def stop(self):
        super().stop()
        with self._lock:
            conns = list(self._boards.values())
            self._boards.clear()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
