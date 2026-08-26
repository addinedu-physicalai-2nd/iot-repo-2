"""
network/UDPModule.py — 영상 파이프라인 (개선판)

핵심 개선점
1) UDP 수신 스레드에서 onFrame()을 직접 실행하지 않고 별도 워커 스레드로 분리
2) 청크를 받을 때마다 마지막 수신 시각을 갱신하여 timeout 오판 방지
3) 패킷/프레임 단위 통계를 분리하여 실제 원인 추적 가능
4) pending frame 한도 및 receive buffer 상향
5) Server -> Admin GUI UDP 송출 시 선택적 micro pacing 적용
6) queue가 밀리면 오래된 프레임을 버리고 최신 프레임 우선 처리

패킷 포맷:
    [frameId(4B)][totalChunks(2B)][chunkIndex(2B)][chunk data]
"""

import queue
import socket
import struct
import threading
import time


# ════════════════════════════════════════════════════════════════
# 공통 패킷 설정
# ════════════════════════════════════════════════════════════════
HEADER = struct.Struct(">IHH")        # frameId, totalChunks, chunkIndex
HEADER_SIZE = HEADER.size             # 8 bytes

# 수신 안정성 설정
FRAME_TIMEOUT = 1.0                   # 마지막 청크 수신 후 폐기 기준(초)
MAX_PENDING = 32                      # 동시에 들고 있을 미완성 프레임 수
RCVBUF = 4 << 20                      # 4MB receive buffer 요청
STATS_SEC = 5.0                       # 통계 출력 주기
FRAME_QUEUE_SIZE = 3                  # onFrame 처리 대기 프레임 수

# 송출 설정
SEND_CHUNK = 1200                     # 일반적인 MTU 아래 유지
SEND_PACING_SEC = 0.0003              # 청크 간 0.3ms. 0이면 pacing 비활성화


# ════════════════════════════════════════════════════════════════
# CamReceiver — ESP32-CAM UDP 청크 수신·재조립
# ════════════════════════════════════════════════════════════════
class CamReceiver:
    def __init__(self, camId: str, listenPort: int, onFrame=None):
        self.camId = camId
        self.listenPort = listenPort
        self.onFrame = onFrame

        self._sock: socket.socket | None = None
        self._running = False

        # frameId -> {
        #   "chunks": {idx: bytes},
        #   "total": int,
        #   "first_t": float,
        #   "last_t": float,
        # }
        self._pending: dict[int, dict] = {}

        # 완성 프레임을 별도 worker로 넘기기 위한 bounded queue
        self._frameQueue: queue.Queue[tuple[str, bytes]] = queue.Queue(
            maxsize=FRAME_QUEUE_SIZE
        )

        # 통계
        self._statDone = 0             # 완성 프레임
        self._statLost = 0             # 폐기 프레임
        self._statBad = 0              # 헤더/JPEG 불량
        self._statPackets = 0          # 수신 UDP datagram 수
        self._statBytes = 0            # 수신 UDP bytes
        self._statDuplicate = 0        # 중복 청크 수
        self._statMissingChunks = 0    # 폐기된 프레임의 누락 청크 합계
        self._statQueueDrop = 0        # onFrame queue가 밀려 버린 완성 프레임
        self._statFramesSeen = 0       # 처음 본 frameId 수
        self._lastCompletedFrameId: int | None = None

    def start(self):
        if self._running:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # 같은 포트에 여러 수신기가 붙어 패킷이 갈리는 상황 방지
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass

        self._sock.bind(("0.0.0.0", self.listenPort))
        self._sock.settimeout(0.2)
        self._running = True

        threading.Thread(
            target=self._recvLoop,
            name=f"CamReceiver-{self.camId}",
            daemon=True,
        ).start()

        if self.onFrame:
            threading.Thread(
                target=self._frameWorker,
                name=f"CamFrameWorker-{self.camId}",
                daemon=True,
            ).start()

        actual_buf = None
        try:
            actual_buf = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except OSError:
            pass

        suffix = f" rcvbuf={actual_buf}" if actual_buf else ""
        print(
            f"[CAM:{self.camId}] UDP 수신 시작 :{self.listenPort} "
            f"(청크 재조립){suffix}"
        )

    def _recvLoop(self):
        lastStat = time.monotonic()

        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                self._dropStale()
                now = time.monotonic()
                if now - lastStat >= STATS_SEC:
                    self._report(now - lastStat)
                    lastStat = now
                continue
            except OSError:
                break

            self._statPackets += 1
            self._statBytes += len(data)
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

        try:
            frameId, total, index = HEADER.unpack_from(data, 0)
        except struct.error:
            self._statBad += 1
            return

        if total == 0 or index >= total:
            self._statBad += 1
            return

        now = time.monotonic()
        entry = self._pending.get(frameId)

        if entry is None:
            if len(self._pending) >= MAX_PENDING:
                self._dropOldest()

            entry = {
                "chunks": {},
                "total": total,
                "first_t": now,
                "last_t": now,
            }
            self._pending[frameId] = entry
            self._statFramesSeen += 1

        elif entry["total"] != total:
            self._statBad += 1
            return
        else:
            # 중요: 최초 시각이 아니라 마지막 패킷 수신 시각을 갱신
            entry["last_t"] = now

        if index in entry["chunks"]:
            self._statDuplicate += 1

        entry["chunks"][index] = data[HEADER_SIZE:]

        if len(entry["chunks"]) != total:
            return

        # 완성 프레임
        del self._pending[frameId]

        try:
            jpeg = b"".join(entry["chunks"][i] for i in range(total))
        except KeyError:
            # 논리적으로 거의 없어야 하지만 방어 코드
            self._statBad += 1
            return

        if len(jpeg) < 4 or jpeg[:2] != b"\xff\xd8" or jpeg[-2:] != b"\xff\xd9":
            self._statBad += 1
            return

        self._statDone += 1
        self._lastCompletedFrameId = frameId

        if self.onFrame:
            self._enqueueFrame(jpeg)

    def _enqueueFrame(self, jpeg: bytes):
        """수신 스레드는 절대 onFrame 때문에 오래 막히지 않는다.

        queue가 꽉 찬 경우 영상 특성상 오래된 완성 프레임을 버리고
        최신 프레임을 넣는다.
        """
        try:
            self._frameQueue.put_nowait((self.camId, jpeg))
            return
        except queue.Full:
            pass

        try:
            self._frameQueue.get_nowait()
            self._frameQueue.task_done()
            self._statQueueDrop += 1
        except queue.Empty:
            pass

        try:
            self._frameQueue.put_nowait((self.camId, jpeg))
        except queue.Full:
            self._statQueueDrop += 1

    def _frameWorker(self):
        while self._running:
            try:
                camId, jpeg = self._frameQueue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self.onFrame(camId, jpeg)
            except Exception as exc:
                print(f"[CAM:{self.camId}] onFrame 오류: {exc}")
            finally:
                self._frameQueue.task_done()

    # ── 미완성 프레임 정리 ───────────────────────────────────────
    def _dropStale(self):
        now = time.monotonic()
        stale = [
            fid for fid, e in self._pending.items()
            if now - e["last_t"] > FRAME_TIMEOUT
        ]

        for fid in stale:
            self._dropFrame(fid)

    def _dropFrame(self, frameId: int):
        entry = self._pending.pop(frameId, None)
        if entry is None:
            return

        received = len(entry["chunks"])
        total = entry["total"]
        self._statMissingChunks += max(0, total - received)
        self._statLost += 1

    def _dropOldest(self):
        if not self._pending:
            return
        oldest = min(self._pending, key=lambda f: self._pending[f]["last_t"])
        self._dropFrame(oldest)

    def _report(self, elapsed: float):
        if elapsed <= 0:
            return

        activity = (
            self._statDone
            or self._statLost
            or self._statBad
            or self._statPackets
            or self._statQueueDrop
        )
        if not activity:
            return

        frame_total = self._statDone + self._statLost
        frame_loss = (
            self._statLost / frame_total * 100.0 if frame_total else 0.0
        )

        # 정확한 packet loss는 송신 측 total packet counter가 없으면 계산 불가능.
        # 대신 폐기된 프레임에서 실제로 빠진 청크 수를 함께 보여준다.
        mbps = self._statBytes * 8 / elapsed / 1_000_000
        pps = self._statPackets / elapsed
        fps = self._statDone / elapsed

        print(
            f"[CAM:{self.camId}] "
            f"{fps:.1f} fps · "
            f"완성 {self._statDone} · "
            f"유실 {self._statLost}({frame_loss:.0f}%) · "
            f"누락청크 {self._statMissingChunks} · "
            f"중복청크 {self._statDuplicate} · "
            f"queueDrop {self._statQueueDrop} · "
            f"불량 {self._statBad} · "
            f"{pps:.0f} pkt/s · {mbps:.2f} Mbps · "
            f"pending {len(self._pending)}"
        )

        self._statDone = 0
        self._statLost = 0
        self._statBad = 0
        self._statPackets = 0
        self._statBytes = 0
        self._statDuplicate = 0
        self._statMissingChunks = 0
        self._statQueueDrop = 0
        self._statFramesSeen = 0

    def stop(self):
        self._running = False

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        self._pending.clear()

        # worker 종료 시 오래된 frame 잔여물 제거
        while True:
            try:
                self._frameQueue.get_nowait()
                self._frameQueue.task_done()
            except queue.Empty:
                break


# ════════════════════════════════════════════════════════════════
# FrameSender — 재조립한 프레임을 Admin GUI 로 UDP 청크 송출
# ════════════════════════════════════════════════════════════════
class FrameSender:
    """구독한 Admin GUI에게 최신 프레임을 UDP로 전송한다.

    기존 API 호환을 유지하면서 청크 폭주를 완화하기 위해 아주 짧은
    pacing 옵션을 적용한다. 네트워크가 충분히 안정적이면
    SEND_PACING_SEC = 0 으로 꺼도 된다.
    """

    def __init__(self, getFrame, defaultFps: int = 12, maxFps: int = 30):
        self.getFrame = getFrame
        self.defaultFps = defaultFps
        self.maxFps = maxFps

        self._sock: socket.socket | None = None
        self._subs: dict[tuple[str, int, str], int] = {}
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        if self._running:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except OSError:
            pass

        self._running = True
        threading.Thread(
            target=self._sendLoop,
            name="FrameSender",
            daemon=True,
        ).start()
        print("[FRAME] UDP 영상 송출 준비")

    # ── 구독 관리 ─────────────────────────────────────────────────
    def subscribe(self, host: str, port: int, camId: str, fps: int | None = None):
        try:
            fps = max(1, min(self.maxFps, int(fps or self.defaultFps)))
        except (TypeError, ValueError):
            fps = self.defaultFps

        with self._lock:
            self._subs[(host, port, camId)] = fps

        print(f"[FRAME] 구독 시작 {host}:{port} cam={camId} fps={fps}")

    def unsubscribe(self, host: str, port: int, camId: str | None = None):
        with self._lock:
            gone = [
                key for key in self._subs
                if key[0] == host
                and key[1] == port
                and (camId is None or key[2] == camId)
            ]
            for key in gone:
                del self._subs[key]

        if gone:
            print(f"[FRAME] 구독 해제 {host}:{port} {camId or '(전체)'}")

    def unsubscribeHost(self, host: str):
        with self._lock:
            gone = [key for key in self._subs if key[0] == host]
            for key in gone:
                del self._subs[key]

        if gone:
            print(f"[FRAME] {host} 구독 {len(gone)}건 정리")

    def subscriptions(self) -> list[tuple[str, int, str]]:
        with self._lock:
            return sorted(self._subs)

    # ── 송출 루프 ─────────────────────────────────────────────────
    def _sendLoop(self):
        lastSeq: dict[tuple[str, int, str], int] = {}
        nextAt: dict[tuple[str, int, str], float] = {}

        while self._running:
            now = time.monotonic()
            with self._lock:
                subs = dict(self._subs)

            for key, fps in subs.items():
                if now < nextAt.get(key, 0.0):
                    continue

                # 누적 오차를 최소화하되 뒤처졌으면 현재 시각 기준으로 재설정
                interval = 1.0 / fps
                scheduled = nextAt.get(key, now)
                nextAt[key] = max(now, scheduled) + interval

                host, port, camId = key
                item = self.getFrame(camId)

                if item is None or item[0] == lastSeq.get(key):
                    continue

                lastSeq[key] = item[0]
                self._sendFrame(host, port, item[0], item[1])

            active_keys = set(subs)
            for key in list(lastSeq):
                if key not in active_keys:
                    lastSeq.pop(key, None)
                    nextAt.pop(key, None)

            time.sleep(0.002)

    def _sendFrame(self, host: str, port: int, frameId: int, jpeg: bytes):
        if not self._sock or not jpeg:
            return

        total = (len(jpeg) + SEND_CHUNK - 1) // SEND_CHUNK
        if total == 0 or total > 0xFFFF:
            return

        for index in range(total):
            start = index * SEND_CHUNK
            end = start + SEND_CHUNK
            packet = (
                HEADER.pack(frameId & 0xFFFFFFFF, total, index)
                + jpeg[start:end]
            )

            try:
                self._sock.sendto(packet, (host, port))
            except OSError:
                return

            # 너무 빠른 burst로 상대 receive buffer를 밀어버리는 현상 완화
            if SEND_PACING_SEC > 0 and index + 1 < total:
                time.sleep(SEND_PACING_SEC)

    def stop(self):
        self._running = False

        with self._lock:
            self._subs.clear()

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
