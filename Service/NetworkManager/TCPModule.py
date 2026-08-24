"""
network/TCPModule.py — TCP 수신 모듈

TCP 로 붙는 상대가 둘이라 서버도 둘이다. 포트를 나눈 이유는:
  - broadcast 가 보드에까지 가면 안 된다(보드는 주문 상태를 알 필요가 없다)
  - Qt 는 clientId 로, 보드는 '이름' 으로 지목해 보낸다

  QtServer  :9000  Qt 클라이언트(대시보드/키오스크)
  BoardHub  :9002  WiFi 제어 보드

둘 다 UTF-8 JSON 한 줄 + '\n' 을 쓰므로 줄 자르기는 JsonLineServer 가 공유한다.

★ 이 스레드들은 orders 를 절대 건드리지 않는다.
  받은 것은 inQueue 에 넣기만 하고, 처리·응답은 CentralControl 이 맡는다.
    Qt   → ("tcp",   clientId,  msg)
    보드 → ("board", boardName, msg)
"""

import json
import socket
import threading
import queue


def encodeLine(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class JsonLineServer:
    """accept 루프 + '\n' 단위 줄 자르기를 공유하는 뼈대."""

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


class QtServer(JsonLineServer):
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


class BoardHub(JsonLineServer):
    """:9002 — WiFi 제어 보드. 접속 직후 이름을 신고해야 한다.

    프로토콜:
      보드 → 서버 : {"hello": "dispenser"}    연결 직후 1회, 이름 신고
                    {"event": "orderComplete", "orderId": 101, "dispensed": [2,1,0]}
      서버 → 보드 : {"cmd": "startOrder", "orderId": 101, "counts": [2,1,0], "slot": 2}

    접속/해제는 서버 내부 이벤트로 큐에 올린다(보드가 보내는 게 아니다):
      {"event": "boardConnected"} / {"event": "boardDisconnected"}
    보드가 리셋되면 여기로 다시 오는데, 그때 서버가 물고 있던 주문을 정리해야 한다.
    """

    def __init__(self, inQueue: queue.Queue, host: str = "0.0.0.0", port: int = 9002):
        super().__init__(host, port, tag="BOARD")
        self.inQueue = inQueue
        self._boards: dict[str, socket.socket] = {}   # boardName -> socket
        self._lock = threading.Lock()

    def _onClient(self, conn: socket.socket, addr):
        name = None
        try:
            for line in self._readLines(conn):
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "hello" in msg:                     # 이름 신고
                    name = str(msg["hello"])
                    self._register(name, conn, addr)
                    self.inQueue.put(("board", name, {"event": "boardConnected"}))
                    continue
                if name is None:
                    # 이름도 안 대고 보낸 건 버린다(어느 보드인지 모르면 처리 불가)
                    print(f"[BOARD] 미등록 보드 {addr} 메시지 무시: {msg}")
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
        with self._lock:
            conn = self._boards.get(name)
        if conn is None:
            print(f"[BOARD] '{name}' 미접속 — 명령 버림: {obj}")
            return False
        try:
            conn.sendall(encodeLine(obj))
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
