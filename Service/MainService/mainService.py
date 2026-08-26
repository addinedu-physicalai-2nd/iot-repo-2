"""
mainService.py — SmartMart Service 의 판단 허브 (진입점 포함)

SW 아키텍처상 SmartMartService 는 세 조각이다:
    MainService     ← 이 파일. 모든 판단과 주문 상태를 소유
    NetworkManager    network/networkManager.py
    DBManager         db/dbManager.py

NetworkManager 와 DBManager 를 써서 모든 판단을 여기서 한다.
Qt 는 NetworkManager 를 통해 받은 정보로 화면만 그린다.

실행:  python mainService.py

동시성 전략:
  - TCP/Serial 스레드는 받은 걸 inQueue 에 넣기만 한다.
  - 이 클래스의 _consumeLoop(단일 스레드) 가 큐를 하나씩 꺼내 처리한다.
  - orders 를 건드리는 건 오직 이 소비 루프뿐 → 경쟁 없음 → Lock 불필요.
  - CAM 은 독립 스레드라 여기 관여하지 않는다.
    프레임은 큐에 들어오지 않고 NetworkManager 가 최신 1장을 들고 있다
    (필요하면 network.getFrame(camId) 로 꺼낸다).

큐에 들어오는 항목 형식: (source, who, msg)
  - ("tcp",   clientId,  {...cmd...})      Qt 클라이언트 요청
  - ("board", boardName, {...event...})    제어 보드 이벤트

보드가 USB(Serial)인지 WiFi(TCP)인지는 여기서 알 필요가 없다.
NetworkManager 가 흡수해서 둘 다 ("board", 이름, msg) 로 올려준다.
"""

import queue
import sys
import time
from collections import deque
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent.parent   # Service/  (DB/, Network/ 가 여기 있음)
_REPO_ROOT = _SERVICE_DIR.parent                         # 저장소 루트 (Library/ 가 여기 있음)
sys.path.insert(0, str(_SERVICE_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from Network.networkManager import NetworkManager
from DB.dbManager import DBManager
from Library.protocol import OrderStatus, FailReason


# 분배 보드 배출구에 담긴 상품 id 를 '순서대로'. startOrder 의 counts 3개가 이 순서다.
# 보드 배선이 바뀌면 여기만 고친다.
DISPENSER_PRODUCTS = [1, 2, 3]

# 픽업박스 개수 (분배 모터가 보낼 수 있는 슬롯 번호 1..N)
SLOT_COUNT = 3

# 보드가 통째로 뻗어도 주문이 갇히지 않게 하는 안전망.
ORDER_TIMEOUT = 60.0    # 출고 지시 후 아무 보고도 없으면 실패로 정리한다

# RFID 카드 태그를 이 시간 안에 안 하면 결제/충전 시도를 포기한다.
PAYMENT_TIMEOUT = 15.0

# 충전 한도. UI/CustomerDashboard/customerQt.py 의 같은 이름 상수와 값이 같아야
# 화면에서 막는 금액과 서버가 막는 금액이 어긋나지 않는다.
MAX_CHARGE_AMOUNT = 500_000     # 한 번에 충전할 수 있는 최대 금액
MAX_CARD_BALANCE = 1_000_000    # 카드가 가질 수 있는 최대 잔액 (ST 는 uint32)


class Order:
    """주문 한 건. MainService 가 메모리에 소유하는 내부 객체다.

    상태는 이 객체가 '정본' 으로 들고, 바뀔 때마다 DBManager 에 위임 저장한다.
    (DB 는 표기·복구용 사본)
    """

    def __init__(self, orderId: int, items: list,
                 status: str = OrderStatus.PENDING, assignedSlot: int | None = None,
                 totalPrice: int = 0, cardId: int | None = None,
                 cardUid: str | None = None, memberName: str | None = None,
                 balanceBefore: int | None = None, balanceAfter: int | None = None,
                 db=None):
        self.orderId = orderId
        self.items = items
        self.status = status
        self.assignedSlot = assignedSlot
        self.totalPrice = totalPrice   # RFID 잔액 결제 시 카드 잔액과 비교하는 데 씀
        self.cardId = cardId           # orders.card_id FK — 결제 검증/저장에 씀
        self.cardUid = cardUid         # 표시·재태그 비교용(카드 물리 UID)
        self.memberName = memberName   # 관리자 화면 표시용
        self.balanceBefore = balanceBefore
        self.balanceAfter = balanceAfter
        self._db = db  # 상태 변경 시 위임 저장할 DBManager 참조

    # ── 상태 변경 → 곧바로 DB에 위임 저장 ────────────────────────
    def setStatus(self, newStatus: str):
        self.status = newStatus
        if self._db:
            self._db.updateOrderStatus(self.orderId, newStatus)

    def assignSlot(self, slot: int):
        self.assignedSlot = slot
        if self._db:
            self._db.assignSlot(self.orderId, slot)

    def releaseSlot(self):
        slot = self.assignedSlot
        self.assignedSlot = None
        if self._db:
            self._db.releaseSlot(slot)


    # ── DB row → Order 객체 복구용 ───────────────────────────────
    @classmethod
    def fromDbRow(cls, row: dict, db=None):
        return cls(
            orderId=row["id"],
            items=row.get("items", []),
            status=row.get("status", OrderStatus.PENDING),
            assignedSlot=row.get("assignedSlot"),
            totalPrice=row.get("totalPrice", 0),
            cardId=row.get("cardId"),
            cardUid=row.get("cardUid"),
            memberName=row.get("memberName"),
            balanceBefore=row.get("balanceBefore"),
            balanceAfter=row.get("balanceAfter"),
            db=db,
        )

    def toDict(self) -> dict:
        return {
            "id": self.orderId,
            "cardUid": self.cardUid,
            "memberName": self.memberName,
            "items": self.items,
            "status": self.status,
            "assignedSlot": self.assignedSlot,
            "totalPrice": self.totalPrice,
        }


class MainService:
    def __init__(self):
        self.db = DBManager()
        self.orders: dict[int, Order] = {}        # 이 루프만 건드림 → Lock 불필요
        self.inQueue: queue.Queue = queue.Queue()

        # 분배 보드는 한 번에 주문 하나만 처리한다.
        # 완료/실패 보고를 받기 전까지 다음 주문을 보내지 않고 여기에 세워둔다.
        self.boardReady = True
        self.waitingOrders: deque[int] = deque()
        # 지금 분배 보드가 물고 있는 주문. 보고가 이 주문 것인지 대조하고,
        # 보드가 리셋되면 이 주문을 실패로 정리하는 데 쓴다.
        self.activeOrderId: int | None = None
        self.activeSince: float = 0.0          # 출고 지시 시각 (ORDER_TIMEOUT 용)

        # 픽업박스 상태. 픽업 보드(Serial)의 slotState 이벤트로 갱신된다.
        # slot -> orderId(사용중) / None(빔). 센서값이 실물 정본이다.
        self.slotOccupied: dict[int, int | None] = {n: None for n in range(1, SLOT_COUNT + 1)}

        # RFID 카드 리더기가 하나뿐이라 아래 둘 중 하나만 동시에 진행된다.
        # 상품 고르기 전 "카드 태그" — 주문 생성에 쓸 cardUid 를 읽어온다.
        # {"clientId", "since"}
        self._pendingCardTag: dict | None = None
        # 결제 확인 — GS 로 같은 카드인지 대조한 뒤 GT→ST 로 잔액 차감.
        # {"orderId", "clientId", "step": "GS"/"GT"/"ST", "since", ["newBalance"]}
        self._pendingPayment: dict | None = None
        # 충전 — 결제와 같은 GS→GT→ST 지만 빼는 대신 더한다. 주문과 무관하다.
        # {"cardUid", "amount", "clientId", "step": "GS"/"GT"/"ST", "since", ...}
        self._pendingCharge: dict | None = None

        self.network = NetworkManager(
            self.inQueue,
            tcpPort=9000,
            boardPort=9002,
            camPorts={"checkout": 6000, "dispensing": 6001},
            # 보드 구성(어느 보드가 USB/WiFi 이고 무슨 명령을 맡는지)은
            # network/networkManager.py 의 BOARDS 에서 정한다.
        )
        self._running = False

    # ── 기동 ─────────────────────────────────────────────────────
    def run(self):
        self.db.initDb()
        self.restoreFromDb()
        self.network.startAll()
        self._running = True
        self._consumeLoop()   # 메인 스레드에서 큐 소비(블로킹)

    # ── 단일 소비 루프: orders 를 혼자 처리 ──────────────────────
    def _consumeLoop(self):
        print("[MS] 소비 루프 시작")
        while self._running:
            try:
                source, clientId, msg = self.inQueue.get(timeout=0.5)
            except queue.Empty:
                self._checkTimeouts()      # 큐가 조용해도 안전망은 돌아야 한다
                continue
            self._checkTimeouts()
            try:
                if source == "tcp":
                    self._handleTcp(clientId, msg)
                elif source == "board":
                    self._handleBoard(clientId, msg)
            except Exception as e:
                print(f"[MS] 처리 오류: {e}")

    # ── DB → 메모리 복구 ─────────────────────────────────────────
    def restoreFromDb(self):
        for status in (OrderStatus.PAID, OrderStatus.DISPATCHING, OrderStatus.PICKUP_READY):
            for row in self.db.getOrdersByStatus(status):
                o = Order.fromDbRow(row, db=self.db)
                self.orders[o.orderId] = o
        print(f"[MS] 진행 중 주문 복구: {len(self.orders)}건")

        # ★ 주문만 복구하고 슬롯 기록을 비워두면, 물건이 함에 남아 있어도
        #   서버는 '빈 칸' 으로 안다. 그러면 손님이 꺼내가도 _completePickup 이
        #   돌지 않아 키오스크가 완료 화면으로 넘어가지 못한다(또 그 칸에
        #   다음 주문을 또 배정한다). 배정돼 있던 슬롯을 같이 되살린다.
        for order in self.orders.values():
            slot = order.assignedSlot
            if slot in self.slotOccupied and order.status != OrderStatus.DONE:
                self.slotOccupied[slot] = order.orderId
                print(f"[MS] 슬롯 {slot} 복구: 주문 {order.orderId} ({order.status})")

    # ── 테스트 데이터 초기화 (resetTestData) ─────────────────────
    def _resetRuntimeState(self):
        """DB 를 비운 직후, 메모리에 남아있는 주문/출고 상태도 같이 비운다."""
        self.orders.clear()
        self.waitingOrders.clear()
        self.boardReady = True
        self.activeOrderId = None
        self.activeSince = 0.0
        self.slotOccupied = {n: None for n in range(1, SLOT_COUNT + 1)}
        print("[MS] 테스트 데이터 초기화 완료")

    # ── TCP 요청 처리 → 응답은 해당 clientId 로 ─────────────────
    def _handleTcp(self, clientId: int, msg: dict):
        cmd = msg.get("cmd")
        resp = None

        if cmd == "getProducts":
            resp = {"cmd": "productList", "items": self.db.getProducts()}

        elif cmd == "tagCard":
            # 상품 고르기 전, 주문에 붙일 cardUid 를 알아내려고 카드를 태그한다.
            # 화면에 잔액도 보여줄 거라 GS 로 UID 읽고 바로 GT 로 잔액도 같이 읽는다.
            if self._readerBusy():
                resp = {"cmd": "cardTagResult", "success": False, "reason": "readerBusy"}
            else:
                self._pendingCardTag = {"clientId": clientId, "step": "GS",
                                         "since": time.monotonic()}
                self.network.sendBoard({"cmd": "getCardStatus"})
                print("[MS] 카드 태그 대기 (주문 시작)")

        elif cmd == "createOrder":
            # cardId/cardUid 는 tagCard 응답으로 클라이언트가 이미 들고 있는 값
            # (등록된 카드인지는 tagCard 때 이미 확인됨 — 여기서 다시 안 물어봄).
            cardId = msg["cardId"]
            ok, orderId, total = self.db.createOrder(cardId, msg["items"])
            if ok:
                self.orders[orderId] = Order(
                    orderId, msg["items"], cardId=cardId, cardUid=msg.get("cardUid"),
                    status=OrderStatus.PENDING, totalPrice=total, db=self.db)
                resp = {"cmd": "orderCreated", "success": True,
                        "orderId": orderId, "totalPrice": total}
            else:
                resp = {"cmd": "orderCreated", "success": False,
                        "reason": "outOfStock"}

        elif cmd == "requestPayment":
            orderId = msg["orderId"]
            order = self.orders.get(orderId)
            if order is None:
                resp = {"cmd": "paymentResult", "orderId": orderId,
                        "status": "fail", "reason": "unknownOrder"}
            elif self._readerBusy():
                # 카드 리더기가 하나뿐이라 동시에 두 건을 처리 못 한다.
                resp = {"cmd": "paymentResult", "orderId": orderId,
                        "status": "fail", "reason": "readerBusy"}
            else:
                # 실제 결제 처리는 비동기(RFID 카드 태그 대기)라 여기서 바로
                # resp 를 안 만든다 — _onRfidResponse 가 끝나고 나서 보낸다.
                self._pendingPayment = {
                    "orderId": orderId, "clientId": clientId,
                    "step": "GS", "since": time.monotonic(),
                }
                self.network.sendBoard({"cmd": "getCardStatus"})
                print(f"[MS] 주문 {orderId} 결제 확인 — 카드 재태그 대기")

        elif cmd == "chargeCard":
            # 충전도 카드에 직접 쓰는 일이라 결제와 똑같이 카드를 다시 태그받는다.
            # (잔액은 DB 가 아니라 카드 안에 있다 — GT 로 읽고 ST 로 되쓴다)
            cardUid = (msg.get("cardUid") or "").lower()
            try:
                amount = int(msg.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if not cardUid:
                resp = {"cmd": "chargeResult", "success": False, "reason": "noCard"}
            elif not 0 < amount <= MAX_CHARGE_AMOUNT:
                resp = {"cmd": "chargeResult", "success": False, "reason": "invalidAmount"}
            elif self._readerBusy():
                resp = {"cmd": "chargeResult", "success": False, "reason": "readerBusy"}
            else:
                self._pendingCharge = {
                    "cardUid": cardUid, "amount": amount, "clientId": clientId,
                    "step": "GS", "since": time.monotonic(),
                }
                self.network.sendBoard({"cmd": "getCardStatus"})
                print(f"[MS] 카드 {cardUid} {amount}원 충전 — 카드 재태그 대기")

        elif cmd == "getHistory":
            resp = {"cmd": "historyData",
                    "orders": self.db.getOrdersByCard(msg["cardUid"])}

        elif cmd == "getAllOrders":
            resp = {"cmd": "allOrdersData", "orders": self.db.getAllOrders()}

        elif cmd == "watchCam":
            # Admin GUI 가 "이 카메라를 내 UDP 포트로 보내달라" 고 신청한다.
            # 보낼 주소는 제어 연결의 IP 를 쓴다(클라이언트가 자기 IP 를 몰라도 되게).
            host = self.network.clientAddress(clientId)
            if host:
                self.network.watchCam(host, msg["udpPort"], msg["camId"], msg.get("fps"))
            resp = {"cmd": "watchCamResult", "camId": msg.get("camId"),
                    "success": bool(host)}

        elif cmd == "unwatchCam":
            host = self.network.clientAddress(clientId)
            if host:
                self.network.unwatchCam(host, msg["udpPort"], msg.get("camId"))
            resp = {"cmd": "unwatchCamResult", "camId": msg.get("camId"),
                    "success": bool(host)}

        elif cmd == "updateStock":
            ok = self.db.updateStock(msg["productId"], msg["newStock"])
            resp = {"cmd": "updateStockResult", "success": ok}

        elif cmd == "resetTestData":
            self.db.resetTestData()
            self._resetRuntimeState()
            resp = {"cmd": "resetTestDataResult", "success": True}
            self.network.broadcastTcp({"cmd": "allOrdersData", "orders": []})

        else:
            resp = {"cmd": "error", "reason": f"unknownCmd:{cmd}"}

        if resp is not None and clientId is not None:
            self.network.sendTo(clientId, resp)

    # ── 보드 이벤트 처리 (USB / WiFi 공통) ───────────────────────
    def _handleBoard(self, boardName: str, msg: dict):
        """어느 보드가 보냈는지가 아니라 '무슨 이벤트인지' 로 분기한다.

        보드별 역할 배정이 바뀌어도(어느 보드에 어느 센서가 붙든) 그대로 동작한다.

        분배 보드(WiFi):
          {"event": "orderComplete", "orderId": 101, "dispensed": [2,1,0]}
          {"event": "orderFailed",   "orderId": 101, "dispensed": [2,0,0],
           "reason": "jam"}          reason 값은 Library.protocol.FailReason
        보고가 곧 '다음 주문 받을 수 있음' 이다(분배 모터 IR 센서가 위치를 보장).
        픽업 보드(Serial):
          {"event": "slotState", "boardId": "pickup", "slot": 1, "occupied": true}
          슬롯 하나가 바뀔 때마다 한 건씩 옴 (PickUpControlBoard.ino 참조).
        """
        self.network.logComm("fromBoard", boardName, msg)

        event = msg.get("event")
        if event == "orderComplete":
            if self._isStale(msg, "orderComplete"):
                return
            self._releaseBoard()
            self._onOrderComplete(msg["orderId"], msg.get("dispensed"))
            self._pumpDispatch()
        elif event == "orderFailed":
            if self._isStale(msg, "orderFailed"):
                return
            self._releaseBoard()
            self._onOrderFailed(msg["orderId"], msg)
            self._pumpDispatch()
        elif event in ("boardConnected", "boardDisconnected"):
            self._onBoardLost(boardName, event)
        elif event == "slotState":
            self._onSlotState(msg.get("slot"), msg.get("occupied"))
        elif event == "rfidResponse":
            self._onRfidResponse(msg)
        elif "hello" in msg:
            # 보드가 방금 부팅했다. 곧 자기 상태를 전부 보고해 온다.
            print(f"[MS] 보드 인사: {msg.get('hello')}")
        else:
            print(f"[MS] 미처리 보드 이벤트 ({boardName}): {msg}")

    def _readerBusy(self) -> bool:
        """카드 리더기가 하나뿐이라 카드 태그/결제 확인/충전을 동시에 못 한다."""
        return (self._pendingCardTag is not None
                or self._pendingPayment is not None
                or self._pendingCharge is not None)

    # ── RFID 응답 라우팅: 카드 태그(주문 시작) vs 결제 확인 중 진행 중인 쪽으로 ──
    def _onRfidResponse(self, msg: dict):
        if self._pendingCardTag is not None:
            self._onCardTagResponse(msg)
        elif self._pendingPayment is not None:
            self._onPaymentRfidResponse(msg)
        elif self._pendingCharge is not None:
            self._onChargeRfidResponse(msg)
        # 셋 다 없으면 늦게 온 응답 등 — 무시

    # ── 카드 태그 (tagCard — 상품 고르기 전, card/member 확인 + 잔액을 읽어온다) ──
    def _onCardTagResponse(self, msg: dict):
        pending = self._pendingCardTag
        if msg.get("cmd") != pending["step"]:
            return

        if pending["step"] == "GS":
            if not msg.get("ok"):
                self._pendingCardTag = None
                self.network.sendTo(pending["clientId"], {
                    "cmd": "cardTagResult", "success": False, "reason": "noCard"})
                return
            card = self.db.getCardByUid(msg["uid"])
            if card is None:
                # 등록 안 된 카드 — 주문 자체를 거부한다.
                self._pendingCardTag = None
                print(f"[MS] 미등록 카드 태그: {msg['uid']}")
                self.network.sendTo(pending["clientId"], {
                    "cmd": "cardTagResult", "success": False, "reason": "cardNotRegistered"})
                return
            pending["card"] = card   # {id, memberId, uid, memberName}
            pending["step"] = "GT"
            self.network.sendBoard({"cmd": "getCardBalance", "uid": card["uid"]})

        elif pending["step"] == "GT":
            self._pendingCardTag = None
            card = pending["card"]
            # 잔액 조회는 실패해도 카드/회원 확인은 이미 끝났으니 태그는 성공으로 친다.
            # 화면엔 잔액이 그냥 안 뜨거나 "-" 로 나온다.
            balance = msg["total"] if msg.get("ok") else None
            print(f"[MS] 카드 태그 완료: {card['uid']} ({card['memberName']}, 잔액: {balance})")
            self.network.sendTo(pending["clientId"], {
                "cmd": "cardTagResult", "success": True,
                "cardId": card["id"], "cardUid": card["uid"],
                "memberName": card["memberName"], "balance": balance})

    # ── 결제 확인 (requestPayment 가 시작해서 GS→GT→ST 순으로 진행) ──
    def _onPaymentRfidResponse(self, msg: dict):
        pending = self._pendingPayment
        if msg.get("cmd") != pending["step"]:
            return  # 우리가 기다리던 응답이 아니다(늦게 온 응답 등) — 무시

        order = self.orders.get(pending["orderId"])
        if order is None:
            self._pendingPayment = None
            return

        if pending["step"] == "GS":
            if not msg.get("ok"):
                self._failPayment("noCard")
                return
            if (order.cardUid or "").lower() != (msg["uid"] or "").lower():
                # 주문 만들 때 태그한 카드랑 다르다 — 남의 카드로 결제 못 하게 막는다.
                # (uid 는 항상 소문자 hex 로 비교 — DB/보드/클라이언트 어디서 대소문자가
                #  섞여도 같은 카드면 같은 카드로 인식되게)
                print(f"[MS] 주문 {order.orderId} 카드 불일치 "
                      f"(주문 카드 {order.cardUid} / 태그된 카드 {msg['uid']})")
                self._failPayment("cardMismatch")
                return
            pending["step"] = "GT"
            self.network.sendBoard({"cmd": "getCardBalance", "uid": order.cardUid})

        elif pending["step"] == "GT":
            if not msg.get("ok"):
                self._failPayment("cardReadError")
                return
            balance = msg["total"]
            if balance < order.totalPrice:
                print(f"[MS] 주문 {order.orderId} 잔액부족 (카드 {balance} / "
                      f"주문 {order.totalPrice}) — 주문 취소")
                self.db.cancelOrder(order.orderId)
                self.orders.pop(order.orderId, None)
                self._failPayment("insufficientBalance")
                return
            pending["balanceBefore"] = balance
            pending["newBalance"] = balance - order.totalPrice
            pending["step"] = "ST"
            self.network.sendBoard({"cmd": "setCardBalance", "uid": order.cardUid,
                                     "total": pending["newBalance"]})

        elif pending["step"] == "ST":
            if not msg.get("ok"):
                # 카드에서 실제로는 안 깎였으니 주문은 살려두고 재시도할 수 있게 한다.
                self._failPayment("cardWriteError")
                return
            self._completePayment()

    def _completePayment(self):
        pending = self._pendingPayment
        self._pendingPayment = None
        order = self.orders.get(pending["orderId"])
        if order is None:
            return
        self.db.confirmPayment(order.orderId, pending["balanceBefore"], pending["newBalance"])
        order.status = OrderStatus.PAID  # confirmPayment 가 이미 DB 에 반영함
        order.balanceBefore = pending["balanceBefore"]
        order.balanceAfter = pending["newBalance"]
        print(f"[MS] 주문 {order.orderId} RFID 결제 완료 (카드 남은 잔액 {pending['newBalance']})")
        self.network.sendTo(pending["clientId"], {
            "cmd": "paymentResult", "orderId": order.orderId, "status": "success",
            "balance": pending["newBalance"]})
        # 보드가 비어 있으면 바로 보내고, 처리 중이면 대기열에 세운다
        self._queueDispatch(order.orderId)

    def _failPayment(self, reason: str):
        pending = self._pendingPayment
        self._pendingPayment = None
        if pending is None:
            return
        print(f"[MS] 주문 {pending['orderId']} 결제 실패: {reason}")
        self.network.sendTo(pending["clientId"], {
            "cmd": "paymentResult", "orderId": pending["orderId"],
            "status": "fail", "reason": reason})

    # ── 충전 (chargeCard 가 시작해서 GS→GT→ST 순으로 진행) ──────
    def _onChargeRfidResponse(self, msg: dict):
        pending = self._pendingCharge
        if msg.get("cmd") != pending["step"]:
            return  # 우리가 기다리던 응답이 아니다(늦게 온 응답 등) — 무시

        if pending["step"] == "GS":
            if not msg.get("ok"):
                self._failCharge("noCard")
                return
            tagged = (msg.get("uid") or "").lower()
            if pending["cardUid"] != tagged:
                # 잔액을 보여준 카드와 지금 올려둔 카드가 다르다 — 남의 카드에
                # 충전되는 걸 막는다. (uid 는 항상 소문자 hex 로 비교)
                print(f"[MS] 충전 카드 불일치 (조회 카드 {pending['cardUid']} / "
                      f"태그된 카드 {tagged})")
                self._failCharge("cardMismatch")
                return
            pending["cardUid"] = tagged   # 리더기가 준 값이 항상 올바른 hex 다
            pending["step"] = "GT"
            self.network.sendBoard({"cmd": "getCardBalance", "uid": tagged})

        elif pending["step"] == "GT":
            if not msg.get("ok"):
                self._failCharge("cardReadError")
                return
            balance = msg["total"]
            newBalance = balance + pending["amount"]
            pending["balanceBefore"] = balance
            pending["newBalance"] = newBalance
            pending["step"] = "ST"
            self.network.sendBoard({"cmd": "setCardBalance",
                                     "uid": pending["cardUid"], "total": newBalance})

        elif pending["step"] == "ST":
            if not msg.get("ok"):
                # 카드에 실제로는 안 써졌다 — 손님 돈이 사라지면 안 되니 실패로 알린다.
                self._failCharge("cardWriteError")
                return
            self._completeCharge()

    def _completeCharge(self):
        pending = self._pendingCharge
        self._pendingCharge = None
        print(f"[MS] 카드 {pending['cardUid']} 충전 완료 "
              f"({pending['balanceBefore']} → {pending['newBalance']})")
        self.network.sendTo(pending["clientId"], {
            "cmd": "chargeResult", "success": True,
            "cardUid": pending["cardUid"], "amount": pending["amount"],
            "balance": pending["newBalance"]})

    def _failCharge(self, reason: str):
        pending = self._pendingCharge
        self._pendingCharge = None
        if pending is None:
            return
        print(f"[MS] 카드 {pending['cardUid']} 충전 실패: {reason}")
        self.network.sendTo(pending["clientId"], {
            "cmd": "chargeResult", "success": False, "reason": reason})

    # ── 출고 시퀀스 ──────────────────────────────────────────────
    # 서버는 '무엇을 몇 개, 어느 슬롯에' 만 정해서 분배 보드에 한 번 보낸다.
    # 배출·컨베이어·분배·개수세기는 보드가 알아서 하고 결과만 보고한다.

    def _queueDispatch(self, orderId: int):
        self.waitingOrders.append(orderId)
        self._pumpDispatch()

    def _pumpDispatch(self):
        """보드가 놀고 있고 빈 슬롯이 있으면 대기열에서 하나 꺼내 보낸다."""
        if not self.boardReady and self.waitingOrders:
            # ★ 여기서 조용히 멈추면 슬롯을 비워도 다음 주문이 영영 안 나간다.
            #   activeOrderId 의 orderComplete/orderFailed 보고가 안 왔다는 뜻이라,
            #   ORDER_TIMEOUT(60초) 안전망이 걸릴 때까지 대기 상태가 이어진다.
            print(f"[MS] boardReady=False — 주문 {list(self.waitingOrders)} 대기 중 "
                  f"(진행중 주문: {self.activeOrderId})")
        while self.boardReady and self.waitingOrders:
            orderId = self.waitingOrders[0]
            order = self.orders.get(orderId)
            if order is None:                     # 사라진 주문은 버린다
                self.waitingOrders.popleft()
                continue

            slot = self._pickFreeSlot()
            if slot is None:
                # 픽업박스가 다 찼다. 손님이 찾아가면(slotReleased) 다시 시도한다.
                print(f"[MS] 빈 픽업박스 없음 — 주문 {orderId} 대기")
                self.network.broadcastTcp({
                    "cmd": "alert", "level": "warn", "orderId": orderId,
                    "message": "픽업 슬롯이 꽉 찼습니다. 잠시 후 다시 시도합니다"})
                return

            counts = self._buildCounts(order)
            if not self.network.startOrder(orderId, counts, slot):
                print(f"[MS] 분배 보드 미접속 — 주문 {orderId} 대기")
                return

            self.waitingOrders.popleft()
            self.boardReady = False               # 보고 올 때까지 잠금
            self.activeOrderId = orderId
            self.activeSince = time.monotonic()
            self.slotOccupied[slot] = orderId     # 슬롯 선점
            order.assignSlot(slot)
            order.setStatus(OrderStatus.DISPATCHING)
            print(f"[MS] 주문 {orderId} 출고 지시: counts={counts} slot={slot}")
            self.network.broadcastTcp(
                {"cmd": "dispatchStatus", "orderId": orderId,
                 "state": OrderStatus.DISPATCHING})

    def _buildCounts(self, order: Order) -> list[int]:
        """order.items 를 DISPENSER_PRODUCTS 순서의 개수 3개로 편다."""
        byProduct = {}
        for item in order.items:
            byProduct[item["productId"]] = byProduct.get(item["productId"], 0) + item["qty"]
        return [byProduct.get(pid, 0) for pid in DISPENSER_PRODUCTS]

    def _pickFreeSlot(self) -> int | None:
        """놓을 픽업박스를 서버가 정한다.

        센서와 모터가 서로 다른 보드에 있어서, 두 정보가 만나는 곳은 서버뿐이다.
        (픽업 보드 Serial → slotOccupied, 분배 모터 ← WiFi startOrder)
        실물 센서값이 정본이고, DB 는 재시작 복구용 기록이다.
        """
        for slot in sorted(self.slotOccupied):
            if self.slotOccupied[slot] is None:
                return slot
        return None

    # ── 보드 보고 처리 ───────────────────────────────────────────
    def _onOrderComplete(self, orderId: int, dispensed: list[int] | None):
        order = self.orders.get(orderId)
        if not order:
            return
        order.setStatus(OrderStatus.PICKUP_READY)
        print(f"[MS] 주문 {orderId} 출고 완료 (배출 {dispensed}) slot={order.assignedSlot}")
        self.network.broadcastTcp(
            {"cmd": "pickupReady", "orderId": orderId, "slot": order.assignedSlot})
        self.network.broadcastTcp(
            {"cmd": "dispatchStatus", "orderId": orderId, "state": OrderStatus.PICKUP_READY})

    def _onOrderFailed(self, orderId: int, msg: dict):
        order = self.orders.get(orderId)
        if not order:
            return
        order.setStatus(OrderStatus.ERROR)
        reason = msg.get("reason", FailReason.UNKNOWN)
        print(f"[MS] 주문 {orderId} 출고 실패: {reason} (배출 {msg.get('dispensed')})")
        # 선점했던 슬롯을 되돌린다
        slot = order.assignedSlot
        if slot in self.slotOccupied and self.slotOccupied[slot] == orderId:
            self.slotOccupied[slot] = None
        order.releaseSlot()
        self.network.broadcastTcp(
            {"cmd": "dispatchStatus", "orderId": orderId, "state": OrderStatus.ERROR})
        self.network.broadcastTcp(
            {"cmd": "alert", "level": "danger",
             "message": f"주문 {orderId} 출고 실패 ({reason})"})
        if slot is not None:
            self.network.broadcastTcp({"cmd": "slotReleased", "slot": slot})

    def _isStale(self, msg: dict, event: str) -> bool:
        """지금 물고 있는 주문의 보고가 맞는지 대조한다.

        보드가 재접속 후 마지막 보고를 재전송하거나, 서버가 이미 실패
        처리한 주문의 뒤늦은 보고가 도착할 수 있다. 그대로 믿으면
        엉뚱한 주문이 완료 처리돼 손님이 빈 박스를 찾아가게 된다.
        """
        reported = msg.get("orderId")
        if reported == self.activeOrderId:
            return False
        print(f"[MS] 철 지난 {event} 무시: 보고 {reported}, 진행중 {self.activeOrderId}")
        return True

    def _releaseBoard(self):
        """보드가 주문 하나를 끝냈다. 보고가 곧 준비 완료 신호다."""
        self.activeOrderId = None
        self.boardReady = True

    def _checkTimeouts(self):
        """보드가 말이 없어도 서버가 멈추지 않게 한다."""
        now = time.monotonic()

        # 출고 지시 후 아무 보고도 없다 → 보드가 뻗었거나 잼. 주문을 놓아준다.
        if self.activeOrderId is not None and now - self.activeSince > ORDER_TIMEOUT:
            print(f"[MS] 보드 무응답 {ORDER_TIMEOUT:.0f}초 — 주문 정리")
            self._failActiveOrder(FailReason.BOARD_TIMEOUT)
            self.boardReady = True
            self._pumpDispatch()

        # 카드를 안 태그하거나 리더기가 응답이 없다 → 결제 시도를 포기한다.
        if (self._pendingPayment is not None
                and now - self._pendingPayment["since"] > PAYMENT_TIMEOUT):
            print(f"[MS] 결제 {PAYMENT_TIMEOUT:.0f}초 무응답 — 결제 정리")
            self._failPayment("cardTimeout")

        # 충전도 카드를 안 올리면 리더기를 붙잡고 있게 되니 같이 정리한다.
        if (self._pendingCharge is not None
                and now - self._pendingCharge["since"] > PAYMENT_TIMEOUT):
            print(f"[MS] 충전 {PAYMENT_TIMEOUT:.0f}초 무응답 — 충전 정리")
            self._failCharge("cardTimeout")

        # 주문 시작용 카드 태그도 마찬가지로 무한 대기하지 않게 한다.
        if (self._pendingCardTag is not None
                and now - self._pendingCardTag["since"] > PAYMENT_TIMEOUT):
            print(f"[MS] 카드 태그 {PAYMENT_TIMEOUT:.0f}초 무응답 — 정리")
            pending = self._pendingCardTag
            self._pendingCardTag = None
            self.network.sendTo(pending["clientId"], {
                "cmd": "cardTagResult", "success": False, "reason": "cardTimeout"})

    def _onBoardLost(self, boardName: str, event: str):
        """보드 접속/재접속.

        출고를 맡은 보드일 때만 진행 중 주문을 정리한다.
        픽업 보드가 잠깐 끊겼다고 분배 보드가 물고 있는 주문을 죽이면 안 된다.
        """
        print(f"[MS] 보드 {boardName} {event}")
        if boardName != self.network.boardFor("startOrder"):
            return
        if self.activeOrderId is not None:
            self._failActiveOrder(FailReason.BOARD_RESET)
        if event == "boardDisconnected":
            self.boardReady = False      # 없는 보드에 주문을 밀어넣지 않는다
        else:
            self.boardReady = True
            self._pumpDispatch()

    def _failActiveOrder(self, reason: str):
        orderId = self.activeOrderId
        self.activeOrderId = None
        if orderId is None:
            return
        print(f"[MS] 주문 {orderId} 보드 응답 없이 중단됨 ({reason})")
        self._onOrderFailed(orderId, {"reason": reason, "dispensed": None})

    # ── 픽업박스 센서 (픽업 보드, Serial) ────────────────────────
    def _onSlotState(self, slot: int | None, occupied: bool | None):
        """픽업박스 센서 하나의 점유 상태. slot 은 1부터, occupied 는 물건 유무.

        PickUpControlBoard.ino 가 슬롯이 바뀔 때마다 한 건씩 보내고, 부팅 직후와
        getSlotState 요청에는 3칸을 전부 보낸다. 비었는데 서버는 주문이
        들어있다고 알던 슬롯 = 손님이 찾아간 것.

        ★ 손님이 꺼냈는데 키오스크가 안 돌아간다면 거의 이 함수다.
          그래서 판단 근거(센서값 / 서버 기록)를 항상 찍는다.
        """
        if slot is None or occupied is None or slot not in self.slotOccupied:
            print(f"[MS] slotState 무시: slot={slot} occupied={occupied} "
                  f"(슬롯 번호는 1~{SLOT_COUNT})")
            return

        held = self.slotOccupied.get(slot)
        print(f"[MS] 슬롯 {slot} 센서: {'물건있음' if occupied else '비었음'} "
              f"(서버 기록: {self._describeHeld(held)})")

        if not occupied:
            if held is None:
                # 기록이 비었다 = 서버 재시작 등으로 매핑을 잃었을 수 있다.
                # 이 슬롯을 배정받은 주문이 아직 살아 있으면 그게 정답이다.
                held = self._orderIdBySlot(slot)
                if held is not None:
                    print(f"[MS] 슬롯 {slot} 기록이 비어 있었음 — 주문 {held} 로 복구")
                    self.slotOccupied[slot] = held
            if held is not None:
                self._completePickup(slot)
            else:
                print(f"[MS] 슬롯 {slot} 은 원래 빈 칸 — 완료 처리할 주문 없음")
        else:
            if held is None:
                # 배정 기록보다 센서가 먼저 올 수도 있으니 주문에서 한 번 찾아본다
                found = self._orderIdBySlot(slot)
                self.slotOccupied[slot] = found if found is not None else -1
                if found is None:
                    print(f"[MS] 슬롯 {slot} 에 서버가 모르는 물건이 놓임")

        self._pumpDispatch()

    def _orderIdBySlot(self, slot: int) -> int | None:
        """그 슬롯을 배정받아 아직 안 끝난 주문. slotOccupied 가 비었을 때의 안전망."""
        for order in self.orders.values():
            if (order.assignedSlot == slot
                    and order.status in (OrderStatus.DISPATCHING, OrderStatus.PICKUP_READY)):
                return order.orderId
        return None

    @staticmethod
    def _describeHeld(held: int | None) -> str:
        if held is None:
            return "빈 칸"
        if held == -1:
            return "모르는 물건"
        return f"주문 {held}"

    def _completePickup(self, slot: int):
        orderId = self.slotOccupied.get(slot)
        self.slotOccupied[slot] = None
        order = self.orders.get(orderId) if isinstance(orderId, int) else None
        if order is None:
            print(f"[MS] 슬롯 {slot} 비워짐 (연결된 주문 없음)")
            self.network.broadcastTcp({"cmd": "slotReleased", "slot": slot})
            return
        order.setStatus(OrderStatus.DONE)
        order.releaseSlot()
        print(f"[MS] 주문 {order.orderId} 픽업 완료 — 슬롯 {slot} 비움")
        self.network.broadcastTcp(
            {"cmd": "dispatchStatus", "orderId": order.orderId, "state": OrderStatus.DONE})
        self.network.broadcastTcp({"cmd": "slotReleased", "slot": slot})
        self.orders.pop(order.orderId, None)

    # ── 영상 ─────────────────────────────────────────────────────
    # 프레임 보관/송출은 NetworkManager 소관이라 여기엔 없다.
    # 필요할 때만 self.network.getFrame(camId) 로 꺼내 쓴다(예: 이상감지).


# ── 진입점 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    system = MainService()
    try:
        system.run()
    except KeyboardInterrupt:
        print("\n종료")
        system._running = False
        system.network.stopAll()
