"""
customerQt.py — 고객용 무인매장 키오스크 (PyQt6)

★ 이 파일은 화면만 그린다. 네트워크는 qtService.QtService 가 전담한다.
  관리자 화면과 같은 서비스를 쓰되, 영상은 쓰지 않아 제어 채널만 연다.

화면 흐름:
  0. 카드 태그  → [카드 태그하기]  (상품 고르기 전에 먼저 태그 — cardUid 확보)
  1. 상품 선택 → 수량 담기 → [주문하기]
  2. 결제      → [카드 결제]        (여기서 같은 카드를 다시 태그해서 확인+차감)
  3. 진행/완료 → 출고중 … "N번 함에서 찾아가세요" … 감사합니다 → 처음(카드 태그)으로

주고받는 명령:
  → tagCard        ← cardTagResult (성공 시 cardUid)
  → getProducts    ← productList
  → createOrder    ← orderCreated   (실패 시 reason: outOfStock)
  → requestPayment ← paymentResult  (카드가 바뀌면 reason: cardMismatch,
                                      잔액부족이면 reason: insufficientBalance)
  ← dispatchStatus / pickupReady / alert    (broadcast push)

★ push 는 모든 Qt 클라이언트에게 broadcast 된다.
  다른 손님 주문까지 날아오므로 반드시 내 orderId 인지 걸러야 한다.

member 테이블은 없다. 주문은 RFID 카드 UID(cardUid)로 식별한다.

실행:  python customerQt.py [--host 127.0.0.1] [--port 9000]
"""

import argparse
import sys
from pathlib import Path

_UI_DIR = Path(__file__).resolve().parent.parent      # UI/  (theme.py, qtService.py 가 여기 있음)
_REPO_ROOT = _UI_DIR.parent                             # 저장소 루트 (Library/ 가 여기 있음)
sys.path.insert(0, str(_UI_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QFrame,
    QVBoxLayout, QHBoxLayout, QStackedWidget,
)
from PyQt6.QtCore import Qt, QTimer

from Library.protocol import OrderStatus
from qtService import QtService
from UI.theme import (
    COL_BG, COL_PANEL, COL_PANEL_HDR, COL_SIDE_SEL, COL_TEXT,
    COL_SUBTLE, COL_LINE, COL_OK, COL_WARN, COL_DANGER,
)

STOCK_POLL_MS = 8000      # 다른 손님이 사가면 재고가 줄어드니 주기적으로 다시 본다
DONE_RESET_MS = 6000      # 완료 화면을 보여준 뒤 처음으로 돌아가기까지


def bigButton(text: str, primary: bool = True) -> QPushButton:
    """터치용 큰 버튼"""
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(64)
    if primary:
        btn.setStyleSheet(
            f"QPushButton{{background:{COL_SIDE_SEL};color:white;border:none;"
            "border-radius:12px;font-size:22px;font-weight:700;padding:0 28px;}"
            "QPushButton:hover{background:#3b7ceb;}"
            f"QPushButton:disabled{{background:{COL_PANEL_HDR};color:{COL_SUBTLE};}}")
    else:
        btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{COL_SUBTLE};"
            f"border:1px solid {COL_LINE};border-radius:12px;font-size:20px;"
            "padding:0 28px;}"
            f"QPushButton:hover{{background:{COL_PANEL};color:{COL_TEXT};}}")
    return btn


class ProductCard(QFrame):
    """상품 한 개. 수량을 담고 빼는 카드."""

    def __init__(self, product: dict, onChange):
        super().__init__()
        self.product = product
        self.qty = 0
        self._onChange = onChange
        # QLabel 이 QFrame 상속이라 QFrame{...} 로 쓰면 자식 라벨까지 테두리가 생긴다.
        # objectName 으로 이 카드만 겨냥한다.
        self.setObjectName("productCard")
        self.setStyleSheet(
            f"QFrame#productCard{{background:{COL_PANEL};"
            f"border:1px solid {COL_LINE};border-radius:14px;}}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        name = QLabel(product.get("name", "?"))
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet(f"background:transparent;color:{COL_TEXT};"
                           "font-size:24px;font-weight:700;")
        lay.addWidget(name)

        price = QLabel(f"{product.get('price', 0):,}원")
        price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:18px;")
        lay.addWidget(price)

        self._stockLabel = QLabel()
        self._stockLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._stockLabel)
        lay.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._minusBtn = self._stepButton("－")
        self._plusBtn = self._stepButton("＋")
        self._minusBtn.clicked.connect(lambda: self._step(-1))
        self._plusBtn.clicked.connect(lambda: self._step(+1))
        self._qtyLabel = QLabel("0")
        self._qtyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qtyLabel.setStyleSheet(f"background:transparent;color:{COL_TEXT};"
                                     "font-size:30px;font-weight:700;")
        row.addWidget(self._minusBtn)
        row.addWidget(self._qtyLabel, 1)
        row.addWidget(self._plusBtn)
        lay.addLayout(row)

        self._refresh()

    def _stepButton(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(58, 58)
        btn.setStyleSheet(
            f"QPushButton{{background:{COL_PANEL_HDR};color:{COL_TEXT};border:none;"
            "border-radius:29px;font-size:24px;font-weight:700;}"
            f"QPushButton:hover{{background:{COL_LINE};}}"
            f"QPushButton:disabled{{background:{COL_BG};color:{COL_LINE};}}")
        return btn

    def _step(self, delta: int):
        self.qty = max(0, min(self.product.get("stock", 0), self.qty + delta))
        self._refresh()
        self._onChange()

    def setProduct(self, product: dict):
        """재고만 갱신. 담아둔 수량이 재고를 넘으면 줄인다."""
        self.product = product
        self.qty = min(self.qty, product.get("stock", 0))
        self._refresh()

    def reset(self):
        self.qty = 0
        self._refresh()

    def _refresh(self):
        stock = self.product.get("stock", 0)
        soldOut = stock <= 0
        color = COL_DANGER if soldOut else (COL_WARN if stock <= 5 else COL_SUBTLE)
        self._stockLabel.setText("품절" if soldOut else f"재고 {stock}개")
        self._stockLabel.setStyleSheet(f"background:transparent;color:{color};"
                                       "font-size:15px;font-weight:600;")
        self._qtyLabel.setText(str(self.qty))
        self._minusBtn.setEnabled(self.qty > 0)
        self._plusBtn.setEnabled(self.qty < stock)


class CustomerKiosk(QMainWindow):
    def __init__(self, host: str = "192.168.0.225", port: int = 9000):
        super().__init__()
        self.setWindowTitle("SmartMart — 주문")
        self.resize(920, 720)
        self.setStyleSheet(f"background:{COL_BG};color:{COL_TEXT};")

        self._products: list[dict] = []
        self._cards: dict[int, ProductCard] = {}    # productId -> 카드
        self._cardId: int | None = None              # 카드 태그로 얻은 카드 id (주문 생성에 씀)
        self._cardUid: str | None = None             # 카드 물리 UID (표시/서버가 재확인용)
        self._memberName: str | None = None          # 헤더에 표시할 회원 이름
        self._cardBalance: int | None = None         # 헤더에 표시할 카드 잔액
        self._orderId: int | None = None            # 내 주문. push 를 거를 기준
        self._orderTotal = 0

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(16)

        outer.addLayout(self._buildHeader())
        self._stack = QStackedWidget()
        self._stack.addWidget(self._pageCardTag())
        self._stack.addWidget(self._pageSelect())
        self._stack.addWidget(self._pagePay())
        self._stack.addWidget(self._pageProgress())
        outer.addWidget(self._stack, 1)
        outer.addWidget(self._statusBar())

        # ── 서버 연결 ────────────────────────────────────────────
        self._net = QtService(host, port, parent=self)
        self._net.connected.connect(self._onConnected)
        self._net.disconnected.connect(self._onDisconnected)
        self._net.message.connect(self._onMessage)

        self._pollTimer = QTimer(self)
        self._pollTimer.timeout.connect(self._requestProducts)
        self._pollTimer.start(STOCK_POLL_MS)

        self._setServerStatus(False)
        self._net.start()

    # ── 상단 ─────────────────────────────────────────────────────
    def _buildHeader(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        logo = QLabel("SmartMart")
        logo.setStyleSheet(f"color:{COL_TEXT};font-size:26px;font-weight:800;")
        self._balanceLabel = QLabel("")   # 카드 태그 전엔 비어있음
        self._balanceLabel.setStyleSheet(
            f"color:{COL_OK};font-size:18px;font-weight:700;")
        self._title = QLabel("카드를 태그해주세요")
        self._title.setStyleSheet(f"color:{COL_SUBTLE};font-size:18px;")
        bar.addWidget(logo)
        bar.addStretch(1)
        bar.addWidget(self._balanceLabel)
        bar.addSpacing(16)
        bar.addWidget(self._title)
        return bar

    def _setBalance(self, balance: int | None):
        self._cardBalance = balance
        self._refreshHeaderInfo()

    def _refreshHeaderInfo(self):
        """헤더의 회원명·잔액 표시를 self._memberName/_cardBalance 로 다시 그린다."""
        parts = []
        if self._memberName:
            parts.append(f"{self._memberName}님")
        if self._cardBalance is not None:
            parts.append(f"잔액 {self._cardBalance:,}원")
        self._balanceLabel.setText(" · ".join(parts))

    # ── 화면 0: 카드 태그 (상품 고르기 전에 cardUid 를 확보) ──────
    def _pageCardTag(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)

        icon = QLabel("💳")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size:64px;")
        lay.addWidget(icon)

        main = QLabel("카드를 태그해주세요")
        main.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main.setWordWrap(True)
        main.setStyleSheet(f"color:{COL_TEXT};font-size:32px;font-weight:800;")
        lay.addWidget(main)

        self._tagHint = QLabel("")
        self._tagHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tagHint.setStyleSheet(f"color:{COL_SUBTLE};font-size:16px;")
        lay.addWidget(self._tagHint)

        lay.addSpacing(24)
        self._tagBtn = bigButton("카드 태그하기")
        self._tagBtn.clicked.connect(self._requestCardTag)
        btnRow = QHBoxLayout()
        btnRow.addStretch(1)
        btnRow.addWidget(self._tagBtn)
        btnRow.addStretch(1)
        lay.addLayout(btnRow)
        lay.addStretch(1)
        return page

    def _requestCardTag(self):
        self._tagBtn.setEnabled(False)
        self._tagHint.setStyleSheet(f"color:{COL_SUBTLE};font-size:16px;")
        self._tagHint.setText("카드를 리더기에 올려주세요…")
        self._net.send({"cmd": "tagCard"})

    # ── 화면 1: 상품 선택 ────────────────────────────────────────
    def _pageSelect(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)

        self._cardRow = QHBoxLayout()
        self._cardRow.setSpacing(18)
        lay.addLayout(self._cardRow, 1)

        foot = QHBoxLayout()
        self._cartLabel = QLabel("담은 상품 없음")
        self._cartLabel.setStyleSheet(f"color:{COL_SUBTLE};font-size:18px;")
        self._totalLabel = QLabel("0원")
        self._totalLabel.setStyleSheet(f"color:{COL_TEXT};font-size:30px;font-weight:800;")
        self._orderBtn = bigButton("주문하기")
        self._orderBtn.setEnabled(False)
        self._orderBtn.clicked.connect(self._goPay)
        foot.addWidget(self._cartLabel)
        foot.addStretch(1)
        foot.addWidget(self._totalLabel)
        foot.addSpacing(20)
        foot.addWidget(self._orderBtn)
        lay.addLayout(foot)
        return page

    def _rebuildCards(self):
        """productList 응답으로 카드를 다시 그린다."""
        # 이미 있는 상품이면 재고만 갱신해 담아둔 수량을 지키지 않는다
        existing = {p["id"] for p in self._products if "id" in p}
        if set(self._cards) == existing and self._cards:
            for product in self._products:
                self._cards[product["id"]].setProduct(product)
            self._refreshCart()
            return

        while self._cardRow.count():
            item = self._cardRow.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self._cards.clear()

        if not self._products:
            hint = QLabel("상품 정보를 불러오는 중입니다…")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"color:{COL_SUBTLE};font-size:18px;")
            self._cardRow.addWidget(hint)
            return

        for product in self._products:
            card = ProductCard(product, self._refreshCart)
            self._cards[product["id"]] = card
            self._cardRow.addWidget(card)
        self._refreshCart()

    def _cartItems(self) -> list[dict]:
        return [{"productId": pid, "qty": card.qty}
                for pid, card in self._cards.items() if card.qty > 0]

    def _refreshCart(self):
        items = self._cartItems()
        total = sum(self._cards[it["productId"]].product.get("price", 0) * it["qty"]
                    for it in items)
        self._orderTotal = total
        if items:
            names = ", ".join(f"{self._cards[it['productId']].product['name']} {it['qty']}개"
                              for it in items)
            self._cartLabel.setText(names)
        else:
            self._cartLabel.setText("담은 상품 없음")
        self._totalLabel.setText(f"{total:,}원")
        self._orderBtn.setEnabled(bool(items) and self._net.isConnected())

    # ── 화면 2: 결제 ─────────────────────────────────────────────
    def _pagePay(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)

        box = QFrame()
        box.setObjectName("payBox")
        box.setStyleSheet(
            f"QFrame#payBox{{background:{COL_PANEL};"
            f"border:1px solid {COL_LINE};border-radius:14px;}}")
        boxLay = QVBoxLayout(box)
        boxLay.setContentsMargins(28, 24, 28, 24)
        boxLay.setSpacing(10)
        head = QLabel("주문 내역")
        head.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};"
                           "font-size:18px;font-weight:600;")
        boxLay.addWidget(head)
        self._payDetail = QLabel("")
        self._payDetail.setStyleSheet(f"background:transparent;color:{COL_TEXT};"
                                      "font-size:22px;line-height:1.6;")
        boxLay.addWidget(self._payDetail)
        boxLay.addStretch(1)
        self._payTotal = QLabel("")
        self._payTotal.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._payTotal.setStyleSheet(f"background:transparent;color:{COL_TEXT};"
                                     "font-size:34px;font-weight:800;")
        boxLay.addWidget(self._payTotal)
        lay.addWidget(box, 1)

        self._payHint = QLabel("")
        self._payHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._payHint.setStyleSheet(f"color:{COL_DANGER};font-size:17px;")
        lay.addWidget(self._payHint)

        row = QHBoxLayout()
        row.setSpacing(14)
        self._cancelBtn = bigButton("취소", primary=False)
        self._cancelBtn.clicked.connect(self._goSelect)
        self._payBtn = bigButton("카드 결제")
        self._payBtn.clicked.connect(self._submitOrder)
        row.addWidget(self._cancelBtn, 1)
        row.addWidget(self._payBtn, 2)
        lay.addLayout(row)
        return page

    def _goPay(self):
        items = self._cartItems()
        if not items:
            return
        lines = [f"{self._cards[it['productId']].product['name']}  ×{it['qty']}"
                 for it in items]
        self._payDetail.setText("\n".join(lines))
        self._payTotal.setText(f"{self._orderTotal:,}원")
        self._payHint.setText("")
        self._payBtn.setEnabled(True)
        self._cancelBtn.setEnabled(True)
        self._showPage(2, "결제")

    def _submitOrder(self):
        """createOrder → (성공하면) requestPayment 로 이어진다."""
        items = self._cartItems()
        if not items:
            return
        self._payBtn.setEnabled(False)
        self._cancelBtn.setEnabled(False)
        self._payHint.setText("")
        self._payBtn.setText("주문 확인 중…")
        self._net.send({"cmd": "createOrder", "cardId": self._cardId,
                           "cardUid": self._cardUid, "items": items})

    def _payFailed(self, message: str):
        self._payHint.setText(message)
        self._payBtn.setText("카드 결제")
        self._payBtn.setEnabled(True)
        self._cancelBtn.setEnabled(True)

    # ── 화면 3: 진행/완료 ────────────────────────────────────────
    def _pageProgress(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)

        self._progressIcon = QLabel("")
        self._progressIcon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._progressIcon.setStyleSheet("font-size:64px;")
        lay.addWidget(self._progressIcon)

        self._progressMain = QLabel("")
        self._progressMain.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._progressMain.setWordWrap(True)
        self._progressMain.setStyleSheet(
            f"color:{COL_TEXT};font-size:40px;font-weight:800;")
        lay.addWidget(self._progressMain)

        self._progressSub = QLabel("")
        self._progressSub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._progressSub.setStyleSheet(f"color:{COL_SUBTLE};font-size:20px;")
        lay.addWidget(self._progressSub)
        lay.addStretch(1)
        return page

    def _setProgress(self, icon: str, main: str, sub: str, color: str = COL_TEXT):
        self._progressIcon.setText(icon)
        self._progressMain.setText(main)
        self._progressMain.setStyleSheet(f"color:{color};font-size:40px;font-weight:800;")
        self._progressSub.setText(sub)

    # ── 화면 전환 ────────────────────────────────────────────────
    def _showPage(self, index: int, title: str):
        self._stack.setCurrentIndex(index)
        self._title.setText(title)

    def _goSelect(self):
        """결제 화면에서 '취소' — 같은 손님이니 카드는 다시 안 태그하고 장바구니로."""
        self._orderId = None
        for card in self._cards.values():
            card.reset()
        self._payBtn.setText("카드 결제")
        self._refreshCart()
        self._showPage(1, "상품을 골라주세요")
        self._requestProducts()

    def _goCardTag(self):
        """주문 완료(픽업까지 끝) 후 처음으로 — 다음 손님을 위해 카드도 새로 태그."""
        self._cardId = None
        self._cardUid = None
        self._memberName = None
        self._orderId = None
        for card in self._cards.values():
            card.reset()
        self._payBtn.setText("카드 결제")
        self._refreshCart()
        self._setBalance(None)
        self._tagBtn.setEnabled(True)
        self._tagHint.setText("")
        self._showPage(0, "카드를 태그해주세요")

    # ── 하단 상태 ────────────────────────────────────────────────
    def _statusBar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(32)
        bar.setStyleSheet(
            f"QFrame#statusBar{{background:{COL_PANEL};border-radius:8px;}}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        self._statusDot = QLabel("●")
        self._statusText = QLabel("")
        self._statusText.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};"
                                       "font-size:13px;")
        lay.addWidget(self._statusDot)
        lay.addWidget(self._statusText)
        lay.addStretch(1)
        return bar

    def _setServerStatus(self, connected: bool):
        self._statusDot.setStyleSheet(
            f"background:transparent;color:{COL_OK if connected else COL_DANGER};")
        self._statusText.setText("연결됨" if connected
                                 else "서버 연결 끊김 — 재연결 시도 중")

    # ── 서버 통신 ────────────────────────────────────────────────
    def _requestProducts(self):
        self._net.send({"cmd": "getProducts"})

    def _onConnected(self):
        self._setServerStatus(True)
        self._requestProducts()
        self._refreshCart()

    def _onDisconnected(self):
        self._setServerStatus(False)
        self._orderBtn.setEnabled(False)

    def _onMessage(self, msg: dict):
        handler = {
            "cardTagResult": self._hCardTagResult,
            "productList":   self._hProductList,
            "orderCreated":  self._hOrderCreated,
            "paymentResult": self._hPaymentResult,
            "dispatchStatus": self._hDispatchStatus,
            "pickupReady":   self._hPickupReady,
            "alert":         self._hAlert,
        }.get(msg.get("cmd"))
        if handler:
            handler(msg)

    def _hCardTagResult(self, msg: dict):
        self._tagBtn.setEnabled(True)
        if not msg.get("success"):
            reason = msg.get("reason", "")
            text = {
                "noCard": "카드를 인식하지 못했습니다. 다시 태그해주세요",
                "cardNotRegistered": "등록되지 않은 카드입니다. 직원에게 문의해주세요",
                "readerBusy": "잠시 후 다시 시도해주세요",
                "cardTimeout": "시간이 초과되었습니다. 다시 시도해주세요",
            }.get(reason, f"카드 인식 실패 ({reason})")
            self._tagHint.setStyleSheet(f"color:{COL_DANGER};font-size:16px;")
            self._tagHint.setText(text)
            return
        self._cardId = msg.get("cardId")
        self._cardUid = msg.get("cardUid")
        self._memberName = msg.get("memberName")
        self._setBalance(msg.get("balance"))
        self._tagHint.setText("")
        self._showPage(1, "상품을 골라주세요")
        self._requestProducts()

    def _hProductList(self, msg: dict):
        self._products = msg.get("items", [])
        self._rebuildCards()

    def _hOrderCreated(self, msg: dict):
        if not msg.get("success"):
            reason = msg.get("reason", "")
            self._payFailed("재고가 부족합니다. 수량을 줄여주세요."
                            if reason == "outOfStock" else f"주문 실패 ({reason})")
            self._requestProducts()      # 남은 재고를 다시 보여준다
            return
        self._orderId = msg.get("orderId")
        self._payBtn.setText("결제 처리 중…")
        self._net.send({"cmd": "requestPayment", "orderId": self._orderId})

    def _hPaymentResult(self, msg: dict):
        if msg.get("orderId") != self._orderId:
            return
        if msg.get("status") != "success":
            reason = msg.get("reason", "")
            text = {
                "insufficientBalance": "카드 잔액이 부족합니다",
                "cardMismatch": "처음 태그한 카드와 다릅니다. 같은 카드로 결제해주세요",
                "noCard": "카드를 인식하지 못했습니다. 다시 태그해주세요",
                "cardTimeout": "카드 태그 시간이 초과되었습니다. 다시 시도해주세요",
                "readerBusy": "잠시 후 다시 시도해주세요",
            }.get(reason, f"결제 실패 ({reason})")
            self._payFailed(text)
            return
        if "balance" in msg:
            self._setBalance(msg["balance"])
        self._setProgress("🧾", "결제가 완료되었습니다",
                          f"주문번호 {self._orderId} · 상품을 준비합니다")
        self._showPage(3, "주문 진행")

    def _hDispatchStatus(self, msg: dict):
        # ★ broadcast 라 다른 손님 주문도 온다. 내 것만 본다.
        if msg.get("orderId") != self._orderId:
            return
        state = msg.get("state")
        if state == OrderStatus.DISPATCHING:
            self._setProgress("📦", "상품을 준비하고 있습니다", "잠시만 기다려주세요")
        elif state == OrderStatus.DONE:
            self._setProgress("🙇", "이용해 주셔서 감사합니다", "곧 처음 화면으로 돌아갑니다")
            QTimer.singleShot(DONE_RESET_MS, self._goCardTag)
        elif state == OrderStatus.ERROR:
            self._setProgress("⚠️", "출고 중 문제가 발생했습니다",
                              "직원을 호출해 주세요", COL_DANGER)

    def _hPickupReady(self, msg: dict):
        if msg.get("orderId") != self._orderId:
            return
        self._setProgress("✅", f"{msg.get('slot')}번 함에서 찾아가세요",
                          "물건을 꺼내시면 주문이 완료됩니다", COL_OK)

    def _hAlert(self, msg: dict):
        # ★ broadcast 라 다른 손님 주문 알림도 온다. 내 것만 본다.
        if msg.get("orderId") != self._orderId:
            return
        self._setProgress("⏳", msg.get("message") or "잠시만 기다려주세요",
                          "곧 이어서 진행됩니다", COL_WARN)

    def closeEvent(self, event):
        self._net.stop()
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description="SmartMart 고객 키오스크")
    ap.add_argument("--host", default="192.168.0.225", help="서버 주소")
    ap.add_argument("--port", type=int, default=9000, help="제어 TCP 포트")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    win = CustomerKiosk(args.host, args.port)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
