"""
network/UDPModule.py — 영상 파이프라인

카메라에서 들어오는 것과 Qt 로 나가는 것을 한곳에 모았다.

  CamReceiver  UDP :6000/:6001  ESP32-CAM 청크 수신·재조립
  FrameSender  UDP 송출          재조립한 프레임을 Admin GUI 로 다시 청크 송출

들어오는 것도 나가는 것도 UDP 다(SW 아키텍처 문서 기준).
받을 쪽은 TCP 제어 채널로 "이 카메라를 이 UDP 포트로 보내달라" 고 신청한다.

패킷 포맷은 카메라가 보내는 것과 같다:
    [frameId(4B)][totalChunks(2B)][chunkIndex(2B)][chunk data]
그래서 수신·재조립 코드를 Qt 쪽에서 그대로 재사용할 수 있다.

두 스레드 모두 orders 를 건드리지 않는다.
"""

import json
import socket
import struct
import threading
import time


# ════════════════════════════════════════════════════════════════
# CamReceiver — ESP32-CAM UDP 청크 수신·재조립
# ════════════════════════════════════════════════════════════════
HEADER = struct.Struct(">IHH")        # frameId, totalChunks, chunkIndex
HEADER_SIZE = HEADER.size             # 8

FRAME_TIMEOUT = 2.0                   # 미완성 프레임 폐기 기준(초)
MAX_PENDING = 8                       # 동시에 들고 있을 미완성 프레임 수
RCVBUF = 1 << 20                      # 1MB — 조각이 몰려 들어올 때 커널 유실 방지
STATS_SEC = 5.0                       # 수신 통계 출력 주기


class CamReceiver:
    def __init__(self, camId: str, listenPort: int,
                 onFrame=None):
        self.camId = camId
        self.listenPort = listenPort
        self.onFrame = onFrame     # 프레임 콜백(예: GUI 전달/녹화). 없으면 무시
        self._sock = None
        self._running = False

        # frameId -> {"chunks": {idx: bytes}, "total": int, "t": float}
        self._pending: dict[int, dict] = {}
        self._statDone = 0          # 완성된 프레임
        self._statLost = 0          # 조각이 모자라 버린 프레임
        self._statBad = 0           # 형식이 깨진 패킷

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR 를 일부러 안 건다. UDP 는 TIME_WAIT 가 없어 필요 없고,
        # 켜두면 다른 테스트 수신기가 같은 포트에 같이 붙어 패킷이 조용히 갈린다.
        # 충돌은 여기서 시끄럽게 실패하는 편이 낫다.
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass                     # 커널이 안 키워주면 그냥 기본값으로 간다
        self._sock.bind(("0.0.0.0", self.listenPort))
        self._sock.settimeout(1.0)   # 타임아웃마다 미완성 프레임 청소
        self._running = True
        threading.Thread(target=self._recvLoop, daemon=True).start()
        print(f"[CAM:{self.camId}] UDP 수신 시작 :{self.listenPort} (청크 재조립)")

    def _recvLoop(self):
        lastStat = time.monotonic()
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                self._dropStale()
                continue
            except OSError:
                break

            self._handlePacket(data)

            now = time.monotonic()
            if now - lastStat >= STATS_SEC:
                self._dropStale()
                self._report(now - lastStat)
                lastStat = now

    # ── 패킷 1개 처리 ────────────────────────────────────────────
    def _handlePacket(self, data: bytes):
        if len(data) <= HEADER_SIZE:
            self._statBad += 1
            return
        frameId, total, index = HEADER.unpack_from(data, 0)
        if total == 0 or index >= total:
            self._statBad += 1      # 말이 안 되는 헤더 — 버린다
            return

        entry = self._pending.get(frameId)
        if entry is None:
            if len(self._pending) >= MAX_PENDING:
                self._dropOldest()
            entry = {"chunks": {}, "total": total, "t": time.monotonic()}
            self._pending[frameId] = entry
        elif entry["total"] != total:
            self._statBad += 1      # 같은 frameId 인데 조각 수가 다르다
            return

        entry["chunks"][index] = data[HEADER_SIZE:]

        if len(entry["chunks"]) == total:
            del self._pending[frameId]
            # index < total 을 이미 걸렀고 개수가 total 이므로 0..total-1 이 모두 있다
            jpeg = b"".join(entry["chunks"][i] for i in range(total))
            if jpeg[:2] != b"\xff\xd8":
                self._statBad += 1  # 합쳤는데 JPEG 가 아니다
                return
            self._statDone += 1
            if self.onFrame:
                # 독립 처리: GUI 전달/녹화 등. orders 와 무관.
                self.onFrame(self.camId, jpeg)

    # ── 미완성 프레임 정리 ───────────────────────────────────────
    def _dropStale(self):
        now = time.monotonic()
        stale = [fid for fid, e in self._pending.items()
                 if now - e["t"] > FRAME_TIMEOUT]
        for fid in stale:
            del self._pending[fid]
        self._statLost += len(stale)

    def _dropOldest(self):
        oldest = min(self._pending, key=lambda f: self._pending[f]["t"])
        del self._pending[oldest]
        self._statLost += 1

    def _report(self, elapsed: float):
        if not (self._statDone or self._statLost or self._statBad):
            return
        total = self._statDone + self._statLost
        loss = (self._statLost / total * 100) if total else 0.0
        print(f"[CAM:{self.camId}] {self._statDone / elapsed:.1f} fps · "
              f"완성 {self._statDone} · 유실 {self._statLost}({loss:.0f}%) · "
              f"불량패킷 {self._statBad}")
        self._statDone = self._statLost = self._statBad = 0

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()
        self._pending.clear()


# ════════════════════════════════════════════════════════════════
# FrameSender — 재조립한 프레임을 Admin GUI 로 UDP 청크 송출
# ════════════════════════════════════════════════════════════════
SEND_CHUNK = 1200            # 카메라와 같은 크기. MTU 안에 들어간다


class FrameSender:
    """구독한 Admin GUI 에게 최신 프레임을 UDP 로 쏜다.

    구독은 TCP 제어 채널로 들어온다(watchCam / unwatchCam).
    UDP 라 상대가 살아있는지 알 수 없으므로, 구독은 명시적으로 끊거나
    제어 연결이 끊길 때 함께 정리한다.
    """

    def __init__(self, getFrame, defaultFps: int = 12, maxFps: int = 30):
        self.getFrame = getFrame          # (camId) -> (seq, jpeg) | None
        self.defaultFps = defaultFps
        self.maxFps = maxFps

        self._sock: socket.socket | None = None
        # (host, port, camId) -> fps
        self._subs: dict[tuple[str, int, str], int] = {}
        self._lock = threading.Lock()     # 구독 목록 보호(orders 와 무관)
        self._running = False

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = True
        threading.Thread(target=self._sendLoop, daemon=True).start()
        print("[FRAME] UDP 영상 송출 준비")

    # ── 구독 관리 (TCP 제어 채널에서 호출) ───────────────────────
    def subscribe(self, host: str, port: int, camId: str, fps: int | None = None):
        try:
            fps = max(1, min(self.maxFps, int(fps or self.defaultFps)))
        except (TypeError, ValueError):
            fps = self.defaultFps
        with self._lock:
            self._subs[(host, port, camId)] = fps
        print(f"[FRAME] 구독 시작 {host}:{port} cam={camId} fps={fps}")

    def unsubscribe(self, host: str, port: int, camId: str | None = None):
        """camId 를 주면 그 카메라만, 없으면 그 주소의 구독 전부 해제."""
        with self._lock:
            gone = [key for key in self._subs
                    if key[0] == host and key[1] == port
                    and (camId is None or key[2] == camId)]
            for key in gone:
                del self._subs[key]
        if gone:
            print(f"[FRAME] 구독 해제 {host}:{port} {camId or '(전체)'}")

    def unsubscribeHost(self, host: str):
        """제어 연결이 끊긴 클라이언트의 구독을 모두 정리한다."""
        with self._lock:
            gone = [key for key in self._subs if key[0] == host]
            for key in gone:
                del self._subs[key]
        if gone:
            print(f"[FRAME] {host} 구독 {len(gone)}건 정리")

    def subscriptions(self) -> list[tuple[str, int, str]]:
        with self._lock:
            return sorted(self._subs)

    # ── 송출 루프 ────────────────────────────────────────────────
    def _sendLoop(self):
        """구독마다 자기 주기로 최신 프레임을 보낸다.

        같은 프레임을 두 번 보내지 않는다(seq 로 판별). 느린 상대 때문에
        루프가 막히지 않도록 UDP 는 그냥 쏘고 잊는다.
        """
        lastSeq: dict[tuple[str, int, str], int] = {}
        nextAt: dict[tuple[str, int, str], float] = {}
        while self._running:
            now = time.monotonic()
            with self._lock:
                subs = dict(self._subs)
            for key, fps in subs.items():
                if now < nextAt.get(key, 0.0):
                    continue
                nextAt[key] = now + 1.0 / fps
                host, port, camId = key
                item = self.getFrame(camId)
                if item is None or item[0] == lastSeq.get(key):
                    continue
                lastSeq[key] = item[0]
                self._sendFrame(host, port, item[0], item[1])
            for key in [k for k in lastSeq if k not in subs]:
                lastSeq.pop(key, None)
                nextAt.pop(key, None)
            time.sleep(0.005)

    def _sendFrame(self, host: str, port: int, frameId: int, jpeg: bytes):
        total = (len(jpeg) + SEND_CHUNK - 1) // SEND_CHUNK
        if total == 0 or total > 0xFFFF:
            return
        for index in range(total):
            packet = (HEADER.pack(frameId & 0xFFFFFFFF, total, index)
                      + jpeg[index * SEND_CHUNK:(index + 1) * SEND_CHUNK])
            try:
                self._sock.sendto(packet, (host, port))
            except OSError:
                return

    def stop(self):
        self._running = False
        with self._lock:
            self._subs.clear()
        if self._sock:
            self._sock.close()
