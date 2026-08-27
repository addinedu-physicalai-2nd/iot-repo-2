"""
customerQt.py — 고객용 무인매장 키오스크 (PyQt6)

★ 이 파일은 화면만 그린다. 네트워크는 qtNetworkManager.QtNetworkManager 가 전담한다.
  관리자 화면과 같은 서비스를 쓰되, 영상은 쓰지 않아 제어 채널만 연다.

화면 흐름:
  0. 홈        → [충전] / [상품 구매] 두 박스 중 하나를 고른다
  1. 카드 태그 → [카드 태그하기]  (두 흐름 모두 여기서 cardUid·잔액을 확보)
       ├ 충전 흐름   → 2. 충전    (잔액 확인 → 금액 입력 → 충전 → 홈)
       └ 구매 흐름   → 3. 상품 선택 → 4. 결제 → 5. 진행/완료 → 홈
  3. 상품 선택 → 수량 담기 → [주문하기]
  4. 결제      → [카드 결제]        (여기서 같은 카드를 다시 태그해서 확인+차감)
  5. 진행/완료 → 출고중 … "N번 함에서 찾아가세요" → 홈으로 (픽업을 안 기다린다)

★ 픽업박스가 3개인 건 '앞 손님이 찾아가기 전에도 다음 주문을 받기' 위해서다.
  그래서 화면은 출고 완료(pickupReady)에서 손을 떼고, 실제 픽업(dispatchStatus
  DONE)은 서버와 픽업 보드가 알아서 끝낸다. 홈으로 돌아간 뒤 오는 내 주문의
  push 는 _orderId 가 이미 None 이라 자연히 걸러진다.

주고받는 명령:
  → tagCard        ← cardTagResult (성공 시 cardUid)
  → chargeCard     ← chargeResult   (같은 카드를 다시 태그해서 확인+충전)
  → getProducts    ← productList
  → createOrder    ← orderCreated   (실패 시 reason: outOfStock)
  → requestPayment ← paymentResult  (카드가 바뀌면 reason: cardMismatch,
                                      잔액부족이면 reason: insufficientBalance)
  ← dispatchStatus / pickupReady / alert    (broadcast push)

★ push 는 모든 Qt 클라이언트에게 broadcast 된다.
  다른 손님 주문까지 날아오므로 반드시 내 orderId 인지 걸러야 한다.

카드 잔액은 DB 가 아니라 RFID 카드 자체에 들어있다. 충전도 결제와 똑같이
리더기에 카드를 올려둔 채로 진행된다(서버가 GS→GT→ST 로 다시 쓴다).

실행:  python customerQt.py [--host 127.0.0.1] [--port 9000]
"""

import argparse
import sys
from pathlib import Path

_UI_DIR = Path(__file__).resolve().parent.parent      # UI/  (theme.py, qtNetworkManager.py 가 여기 있음)
_REPO_ROOT = _UI_DIR.parent                             # 저장소 루트 (Library/ 가 여기 있음)
sys.path.insert(0, str(_UI_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QFrame,
    QVBoxLayout, QHBoxLayout, QStackedWidget, QLineEdit,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIntValidator

from Library.protocol import OrderStatus
from qtNetworkManager import QtNetworkManager
from UI.theme import (
    COL_BG, COL_PANEL, COL_PANEL_HDR, COL_SIDE_SEL, COL_TEXT,
    COL_SUBTLE, COL_LINE, COL_OK, COL_WARN, COL_DANGER,
)

STOCK_POLL_MS = 8000      # 다른 손님이 사가면 재고가 줄어드니 주기적으로 다시 본다

# ★ 픽업박스가 3개인 이유가 '먼저 산 손님이 찾아가기 전에도 다음 주문을 받는' 것이다.
#   그래서 손님이 물건을 꺼낼 때(dispatchStatus DONE)까지 기다리지 않고,
#   출고가 끝나 함에 물건이 놓인 시점(pickupReady)에 화면을 놓아준다.
#   서버는 원래부터 3건 동시 진행이 됐고(boardReady 는 출고 완료 때 풀린다),
#   막고 있던 건 이 화면뿐이었다.
PICKUP_RESET_MS = 10000   # "N번 함에서 찾아가세요" 를 보여준 뒤 홈으로
DONE_RESET_MS = 4000      # 손님이 그 자리에서 바로 꺼낸 경우(감사합니다) 홈으로
ERROR_RESET_MS = 20000    # 출고 실패 화면에서 홈으로 — 키오스크가 갇히지 않게

# ── 화면 번호 (QStackedWidget 인덱스) ────────────────────────────
PAGE_HOME = 0
PAGE_TAG = 1
PAGE_CHARGE = 2
PAGE_SELECT = 3
PAGE_PAY = 4
PAGE_PROGRESS = 5

# ── 카드 태그 후 어디로 갈지 ─────────────────────────────────────
FLOW_CHARGE = "charge"
FLOW_PURCHASE = "purchase"

# 충전 금액에 한도를 두지 않는다. 화면이 막는 건 0원 이하뿐이고, 나머지는
# 서버에 맡긴다(카드의 4바이트 잔액 필드를 넘는 경우만 거절된다).
QUICK_AMOUNTS = (1_000, 5_000, 10_000, 50_000)

# 홈 화면 박스 높이 상한. 아래 '찾아가실 물건' 스트립 자리를 남기려고 줄여둔다.
HOME_BOX_MAX_H = 330


def _clearLayout(layout):
    """레이아웃을 비운다.

    ★ item.widget() 을 두 번 부르면 안 된다 — setParent(None) 뒤에는 None 이
      돌아와서 AttributeError 가 난다. 한 번 꺼내 변수에 들고 쓴다.
      (addStretch 로 넣은 spacer 는 widget() 이 None 이라 그냥 버려진다)
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


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


class HomeBox(QFrame):
    """홈 화면의 큰 선택 박스. 박스 아무 데나 누르면 onClick 이 불린다."""

    def __init__(self, icon: str, title: str, desc: str, onClick):
        super().__init__()
        self._onClick = onClick
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # QLabel 도 QFrame 상속이라 QFrame{...} 로 쓰면 자식 라벨까지 테두리가 생긴다.
        # objectName 으로 이 박스만 겨냥한다.
        self.setObjectName("homeBox")
        self._paint(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 22, 20, 22)
        lay.setSpacing(8)
        lay.addStretch(1)

        iconLabel = QLabel(icon)
        iconLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        iconLabel.setStyleSheet("background:transparent;font-size:56px;")
        lay.addWidget(iconLabel)

        titleLabel = QLabel(title)
        titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        titleLabel.setStyleSheet(f"background:transparent;color:{COL_TEXT};"
                                 "font-size:30px;font-weight:800;")
        lay.addWidget(titleLabel)

        descLabel = QLabel(desc)
        descLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        descLabel.setWordWrap(True)
        descLabel.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:17px;")
        lay.addWidget(descLabel)
        lay.addStretch(1)

    def _paint(self, hover: bool):
        border = COL_SIDE_SEL if hover else COL_LINE
        background = COL_PANEL_HDR if hover else COL_PANEL
        self.setStyleSheet(
            f"QFrame#homeBox{{background:{background};"
            f"border:2px solid {border};border-radius:20px;}}")

    def enterEvent(self, event):
        self._paint(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._paint(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._onClick()
        super().mouseReleaseEvent(event)


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
        self._slots: dict[int, dict] = {}            # slot -> 픽업박스 상태 (홈 하단 표시용)
        self._flow: str | None = None                # 카드 태그 후 갈 곳 (충전/구매)
        self._cardId: int | None = None              # 카드 태그로 얻은 카드 id (주문 생성에 씀)
        self._cardUid: str | None = None             # 카드 물리 UID (표시/서버가 재확인용)
        self._memberName: str | None = None          # 헤더에 표시할 회원 이름
        self._cardBalance: int | None = None         # 헤더에 표시할 카드 잔액
        self._orderId: int | None = None            # 내 주문. push 를 거를 기준
        self._orderTotal = 0
        self._chargePending = False                  # 충전 응답 대기 중 (중복 요청 방지)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(16)

        outer.addLayout(self._buildHeader())
        self._stack = QStackedWidget()
        self._stack.addWidget(self._pageHome())       # PAGE_HOME
        self._stack.addWidget(self._pageCardTag())    # PAGE_TAG
        self._stack.addWidget(self._pageCharge())     # PAGE_CHARGE
        self._stack.addWidget(self._pageSelect())     # PAGE_SELECT
        self._stack.addWidget(self._pagePay())        # PAGE_PAY
        self._stack.addWidget(self._pageProgress())   # PAGE_PROGRESS
        outer.addWidget(self._stack, 1)
        outer.addWidget(self._statusBar())

        # ── 서버 연결 ────────────────────────────────────────────
        self._net = QtNetworkManager(host, port, parent=self)
        self._net.connected.connect(self._onConnected)
        self._net.disconnected.connect(self._onDisconnected)
        self._net.message.connect(self._onMessage)

        self._pollTimer = QTimer(self)
        self._pollTimer.timeout.connect(self._pollProducts)
        self._pollTimer.start(STOCK_POLL_MS)

        # 진행 화면에서 홈으로 자동 복귀시키는 타이머.
        # singleShot 을 그때그때 걸면 취소를 못 해서 겹쳐 터진다 — 하나만 두고 재시작한다.
        self._homeTimer = QTimer(self)
        self._homeTimer.setSingleShot(True)
        self._homeTimer.timeout.connect(self._goHome)
        self._countdownTimer = QTimer(self)
        self._countdownTimer.setInterval(1000)
        self._countdownTimer.timeout.connect(self._tickCountdown)
        self._homeLeft = 0
        self._homeSub = ""

        self._setServerStatus(False)
        self._showPage(PAGE_HOME, "무엇을 도와드릴까요?")
        self._net.start()

    # ── 상단 ─────────────────────────────────────────────────────
    def _buildHeader(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        logo = QLabel("SmartMart")
        logo.setStyleSheet(f"color:{COL_TEXT};font-size:26px;font-weight:800;")
        self._balanceLabel = QLabel("")   # 카드 태그 전엔 비어있음
        self._balanceLabel.setStyleSheet(
            f"color:{COL_OK};font-size:18px;font-weight:700;")
        self._title = QLabel("무엇을 도와드릴까요?")
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

    # ── 화면 0: 홈 (충전 / 상품 구매) ────────────────────────────
    def _pageHome(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)

        head = QLabel("원하시는 메뉴를 선택해주세요")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setStyleSheet(f"color:{COL_TEXT};font-size:30px;font-weight:800;")
        lay.addWidget(head)

        row = QHBoxLayout()
        row.setSpacing(24)
        for box in (HomeBox("💰", "충전", "카드에 금액을 채웁니다",
                            lambda: self._startFlow(FLOW_CHARGE)),
                    HomeBox("🛒", "상품 구매", "상품을 고르고 카드로 결제합니다",
                            lambda: self._startFlow(FLOW_PURCHASE))):
            # 아래 '찾아가실 물건' 스트립이 눌리지 않게 박스 키를 제한한다
            box.setMaximumHeight(HOME_BOX_MAX_H)
            row.addWidget(box, 1)
        lay.addLayout(row, 1)

        lay.addWidget(self._buildPickupStrip())
        return page

    # ── 홈 하단: 찾아가실 물건 (픽업 대기 중인 함) ───────────────
    def _buildPickupStrip(self) -> QWidget:
        """★ 출고가 끝나면 화면이 홈으로 돌아가버려서, 손님이 '몇 번 함이었지'
        를 확인할 데가 없다. 그래서 홈에 픽업 대기 중인 함을 계속 띄워둔다.

        서버가 pickupReady / slotReleased 를 모든 클라이언트에 broadcast 하므로
        따로 폴링하지 않는다. 방금 켜졌을 때만 getSlots 로 현재 상태를 맞춘다.
        """
        box = QFrame()
        box.setObjectName("pickupStrip")
        box.setStyleSheet(
            f"QFrame#pickupStrip{{background:{COL_PANEL};"
            f"border:1px solid {COL_LINE};border-radius:14px;}}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(20, 12, 20, 14)
        lay.setSpacing(8)

        cap = QLabel("찾아가실 물건")
        cap.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};"
                          "font-size:16px;font-weight:600;")
        lay.addWidget(cap)

        self._slotRow = QHBoxLayout()
        self._slotRow.setSpacing(12)
        lay.addLayout(self._slotRow)
        self._renderSlots()
        return box

    def _renderSlots(self):
        """self._slots 로 함 칩을 다시 그린다."""
        _clearLayout(self._slotRow)

        if not self._slots:
            hint = QLabel("픽업박스 상태를 확인하는 중입니다…")
            hint.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:15px;")
            self._slotRow.addWidget(hint)
            self._slotRow.addStretch(1)
            return

        for slot in sorted(self._slots):
            self._slotRow.addWidget(self._slotChip(self._slots[slot]), 1)

    def _slotChip(self, state: dict) -> QWidget:
        occupied = state.get("occupied")
        orderId = state.get("orderId")
        if not occupied:
            status, color = "비어있음", COL_SUBTLE
        elif orderId:
            status, color = f"주문 {orderId} 대기중", COL_OK
        else:
            # 서버가 모르는 물건(재시작 등). 칸은 차 있으니 비었다고 하면 안 된다.
            status, color = "사용 중", COL_WARN

        chip = QFrame()
        chip.setObjectName("slotChip")
        chip.setStyleSheet(
            f"QFrame#slotChip{{background:{COL_BG};"
            f"border:1px solid {color if occupied else COL_LINE};border-radius:10px;}}")
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)

        num = QLabel(f"{state.get('slot')}번")
        num.setStyleSheet(f"background:transparent;color:{color};"
                          "font-size:22px;font-weight:800;")
        text = QLabel(status)
        text.setStyleSheet(f"background:transparent;color:{color};font-size:15px;")
        lay.addWidget(num)
        lay.addWidget(text)
        lay.addStretch(1)
        return chip

    def _startFlow(self, flow: str):
        """홈에서 충전/구매를 고르면 둘 다 카드 태그 화면부터 시작한다."""
        self._flow = flow
        self._tagBtn.setEnabled(True)
        self._tagHint.setStyleSheet(f"color:{COL_SUBTLE};font-size:16px;")
        self._tagHint.setText("")
        if flow == FLOW_CHARGE:
            self._tagMain.setText("충전할 카드를 태그해주세요")
            self._showPage(PAGE_TAG, "충전 — 카드 태그")
        else:
            self._tagMain.setText("결제할 카드를 태그해주세요")
            self._showPage(PAGE_TAG, "상품 구매 — 카드 태그")

    # ── 화면 1: 카드 태그 (충전·구매 둘 다 여기서 cardUid 를 확보) ──
    def _pageCardTag(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)

        icon = QLabel("💳")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size:64px;")
        lay.addWidget(icon)

        self._tagMain = QLabel("카드를 태그해주세요")
        self._tagMain.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tagMain.setWordWrap(True)
        self._tagMain.setStyleSheet(f"color:{COL_TEXT};font-size:32px;font-weight:800;")
        lay.addWidget(self._tagMain)

        self._tagHint = QLabel("")
        self._tagHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tagHint.setStyleSheet(f"color:{COL_SUBTLE};font-size:16px;")
        lay.addWidget(self._tagHint)

        lay.addSpacing(24)
        self._tagBtn = bigButton("카드 태그하기")
        self._tagBtn.clicked.connect(self._requestCardTag)
        tagBack = bigButton("처음으로", primary=False)
        tagBack.clicked.connect(self._goHome)
        btnRow = QHBoxLayout()
        btnRow.setSpacing(14)
        btnRow.addStretch(1)
        btnRow.addWidget(tagBack)
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

    # ── 화면 2: 충전 ─────────────────────────────────────────────
    def _pageCharge(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        box = QFrame()
        box.setObjectName("chargeBox")
        box.setStyleSheet(
            f"QFrame#chargeBox{{background:{COL_PANEL};"
            f"border:1px solid {COL_LINE};border-radius:14px;}}")
        boxLay = QVBoxLayout(box)
        boxLay.setContentsMargins(28, 24, 28, 24)
        boxLay.setSpacing(14)

        self._chargeOwner = QLabel("")
        self._chargeOwner.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};"
                                        "font-size:18px;font-weight:600;")
        boxLay.addWidget(self._chargeOwner)

        boxLay.addLayout(self._chargeRow("현재 잔액", "_curBalanceLabel",
                                         COL_TEXT, 32))

        amountCap = QLabel("충전할 금액")
        amountCap.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:18px;")
        boxLay.addWidget(amountCap)

        self._amountEdit = QLineEdit()
        self._amountEdit.setPlaceholderText("0")
        self._amountEdit.setAlignment(Qt.AlignmentFlag.AlignRight)
        validator = QIntValidator(self)
        validator.setBottom(0)             # 상한 없음 — 음수만 막는다
        self._amountEdit.setValidator(validator)
        self._amountEdit.setMinimumHeight(64)
        self._amountEdit.setStyleSheet(
            f"QLineEdit{{background:{COL_BG};color:{COL_TEXT};"
            f"border:1px solid {COL_LINE};border-radius:12px;"
            "font-size:32px;font-weight:800;padding:0 18px;}")
        self._amountEdit.textChanged.connect(self._refreshChargeTotal)
        boxLay.addWidget(self._amountEdit)

        quickRow = QHBoxLayout()
        quickRow.setSpacing(10)
        for amount in QUICK_AMOUNTS:
            btn = self._quickButton(f"+{amount:,}")
            btn.clicked.connect(lambda _, a=amount: self._addAmount(a))
            quickRow.addWidget(btn, 1)
        clearBtn = self._quickButton("지우기")
        clearBtn.clicked.connect(lambda: self._amountEdit.setText(""))
        quickRow.addWidget(clearBtn, 1)
        boxLay.addLayout(quickRow)

        boxLay.addStretch(1)
        boxLay.addLayout(self._chargeRow("충전 후 잔액", "_finalBalanceLabel",
                                         COL_OK, 38))
        lay.addWidget(box, 1)

        self._chargeHint = QLabel("")
        self._chargeHint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chargeHint.setStyleSheet(f"color:{COL_DANGER};font-size:17px;")
        lay.addWidget(self._chargeHint)

        row = QHBoxLayout()
        row.setSpacing(14)
        self._chargeHomeBtn = bigButton("처음으로", primary=False)
        self._chargeHomeBtn.clicked.connect(self._goHome)
        self._chargeBtn = bigButton("충전하기")
        self._chargeBtn.clicked.connect(self._submitCharge)
        row.addWidget(self._chargeHomeBtn, 1)
        row.addWidget(self._chargeBtn, 2)
        lay.addLayout(row)
        return page

    def _chargeRow(self, caption: str, attr: str, color: str, size: int) -> QHBoxLayout:
        """'현재 잔액 ........ 12,000원' 같은 한 줄. 값 라벨을 attr 이름으로 달아둔다."""
        row = QHBoxLayout()
        cap = QLabel(caption)
        cap.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:18px;")
        value = QLabel("-")
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        value.setStyleSheet(f"background:transparent;color:{color};"
                            f"font-size:{size}px;font-weight:800;")
        setattr(self, attr, value)
        row.addWidget(cap)
        row.addStretch(1)
        row.addWidget(value)
        return row

    def _quickButton(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(54)
        btn.setStyleSheet(
            f"QPushButton{{background:{COL_PANEL_HDR};color:{COL_TEXT};border:none;"
            "border-radius:12px;font-size:18px;font-weight:700;}"
            f"QPushButton:hover{{background:{COL_LINE};}}")
        return btn

    def _chargeAmount(self) -> int:
        text = self._amountEdit.text().strip()
        return int(text) if text.isdigit() else 0

    def _addAmount(self, delta: int):
        self._amountEdit.setText(str(self._chargeAmount() + delta))

    def _enterCharge(self):
        """카드 태그가 끝나고 충전 화면으로 들어올 때 한 번."""
        owner = f"{self._memberName}님의 카드" if self._memberName else "카드"
        self._chargeOwner.setText(f"{owner} · {self._cardUid or '-'}")
        self._chargePending = False
        self._amountEdit.setText("")
        self._chargeHint.setText("")
        self._chargeBtn.setText("충전하기")
        self._refreshChargeTotal()
        self._showPage(PAGE_CHARGE, "충전")

    def _refreshChargeTotal(self):
        """현재 잔액·충전 후 잔액 표시와 [충전하기] 활성 여부를 다시 계산한다."""
        balance = self._cardBalance
        amount = self._chargeAmount()
        self._curBalanceLabel.setText("-" if balance is None else f"{balance:,}원")
        if balance is None:
            # 잔액을 못 읽으면 충전 후 금액을 보여줄 수 없다(서버도 GT 부터 다시 한다).
            self._finalBalanceLabel.setText("-")
            self._chargeBtn.setEnabled(False)
            return
        self._finalBalanceLabel.setText(f"{balance + amount:,}원")
        # ★ 충전 응답을 기다리는 동안엔 금액을 만져도 버튼이 다시 켜지면 안 된다.
        #   (같은 카드에 두 번 써질 수 있다)
        self._chargeBtn.setEnabled(amount > 0 and not self._chargePending
                                   and self._net.isConnected())

    def _submitCharge(self):
        amount = self._chargeAmount()
        if amount <= 0:
            return
        self._chargePending = True
        self._chargeBtn.setEnabled(False)
        self._chargeHomeBtn.setEnabled(True)   # 충전 중에도 나갈 수는 있게 둔다
        self._chargeBtn.setText("충전 처리 중…")
        self._chargeHint.setStyleSheet(f"color:{COL_SUBTLE};font-size:17px;")
        self._chargeHint.setText("카드를 리더기에 올려주세요…")
        self._net.send({"cmd": "chargeCard", "cardId": self._cardId,
                        "cardUid": self._cardUid, "amount": amount})

    def _chargeFailed(self, message: str):
        self._chargePending = False
        self._chargeBtn.setText("충전하기")
        self._refreshChargeTotal()
        # 한도 안내를 덮어써야 하니 _refreshChargeTotal 뒤에 쓴다
        self._chargeHint.setStyleSheet(f"color:{COL_DANGER};font-size:17px;")
        self._chargeHint.setText(message)

    # ── 화면 3: 상품 선택 ────────────────────────────────────────
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
        selectHome = bigButton("처음으로", primary=False)
        selectHome.clicked.connect(self._goHome)
        self._orderBtn = bigButton("주문하기")
        self._orderBtn.setEnabled(False)
        self._orderBtn.clicked.connect(self._goPay)
        foot.addWidget(self._cartLabel)
        foot.addStretch(1)
        foot.addWidget(self._totalLabel)
        foot.addSpacing(20)
        foot.addWidget(selectHome)
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

        _clearLayout(self._cardRow)
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

    # ── 화면 4: 결제 ─────────────────────────────────────────────
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
        self._showPage(PAGE_PAY, "결제")

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

    # ── 화면 5: 진행/완료 ────────────────────────────────────────
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

        lay.addSpacing(24)
        # 자동 복귀를 기다리기 싫은 손님(=뒤에 줄 선 손님)이 바로 넘길 수 있게.
        # 화면을 놓아줘도 되는 상태일 때만 보인다.
        self._progressHomeBtn = bigButton("처음으로", primary=False)
        self._progressHomeBtn.clicked.connect(self._goHome)
        self._progressHomeBtn.hide()
        btnRow = QHBoxLayout()
        btnRow.addStretch(1)
        btnRow.addWidget(self._progressHomeBtn)
        btnRow.addStretch(1)
        lay.addLayout(btnRow)
        lay.addStretch(1)
        return page

    def _setProgress(self, icon: str, main: str, sub: str, color: str = COL_TEXT):
        self._progressIcon.setText(icon)
        self._progressMain.setText(main)
        self._progressMain.setStyleSheet(f"color:{color};font-size:40px;font-weight:800;")
        self._progressSub.setText(sub)

    # ── 진행 화면 자동 복귀 ──────────────────────────────────────
    def _scheduleHome(self, delayMs: int, sub: str):
        """delayMs 뒤에 홈으로 돌아간다. 남은 시간을 아래줄에 같이 보여준다."""
        self._homeSub = sub
        self._homeLeft = delayMs // 1000
        self._homeTimer.start(delayMs)
        self._countdownTimer.start()
        self._progressHomeBtn.show()
        self._paintCountdown()

    def _cancelHome(self):
        self._homeTimer.stop()
        self._countdownTimer.stop()
        self._progressHomeBtn.hide()

    def _tickCountdown(self):
        self._homeLeft = max(0, self._homeLeft - 1)
        self._paintCountdown()

    def _paintCountdown(self):
        tail = f"{self._homeLeft}초 후 처음 화면으로 돌아갑니다"
        self._progressSub.setText(f"{self._homeSub} · {tail}" if self._homeSub else tail)

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
        self._showPage(PAGE_SELECT, "상품을 골라주세요")
        self._requestProducts()

    def _goHome(self):
        """볼일이 끝났다(또는 손님이 그만뒀다) — 다음 손님을 위해 전부 비운다.

        ★ 아직 함에 안 찾아간 물건이 있어도 여기로 온다. 내 주문 추적을 놓는
          것뿐이고, 남은 픽업은 서버와 픽업 보드가 알아서 끝낸다.
        """
        self._cancelHome()
        self._flow = None
        self._cardId = None
        self._cardUid = None
        self._memberName = None
        self._orderId = None
        for card in self._cards.values():
            card.reset()
        self._payBtn.setText("카드 결제")
        self._chargePending = False
        self._chargeBtn.setText("충전하기")
        self._chargeHint.setText("")
        self._amountEdit.setText("")
        self._refreshCart()
        self._setBalance(None)
        self._tagBtn.setEnabled(True)
        self._tagHint.setText("")
        self._showPage(PAGE_HOME, "무엇을 도와드릴까요?")
        # 손님이 홈으로 돌아올 때마다 함 상태를 서버 값으로 한 번 맞춘다.
        # (broadcast 로도 따라가지만, 여기가 어긋남을 스스로 고치는 지점이 된다)
        self._requestSlots()

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

    def _requestSlots(self):
        self._net.send({"cmd": "getSlots"})

    def _pollProducts(self):
        """재고는 상품 화면을 보고 있을 때만 다시 물어본다(충전 중엔 의미 없음)."""
        if self._stack.currentIndex() == PAGE_SELECT:
            self._requestProducts()

    def _onConnected(self):
        self._setServerStatus(True)
        self._requestProducts()
        self._requestSlots()      # 끊긴 사이 놓친 broadcast 를 여기서 따라잡는다
        self._refreshCart()
        self._refreshChargeTotal()

    def _onDisconnected(self):
        self._setServerStatus(False)
        self._orderBtn.setEnabled(False)
        self._chargeBtn.setEnabled(False)

    def _onMessage(self, msg: dict):
        handler = {
            "cardTagResult": self._hCardTagResult,
            "chargeResult":  self._hChargeResult,
            "productList":   self._hProductList,
            "orderCreated":  self._hOrderCreated,
            "paymentResult": self._hPaymentResult,
            "dispatchStatus": self._hDispatchStatus,
            "pickupReady":   self._hPickupReady,
            "slotData":      self._hSlotData,
            "slotReleased":  self._hSlotReleased,
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
        if self._flow == FLOW_CHARGE:
            self._enterCharge()
            return
        self._showPage(PAGE_SELECT, "상품을 골라주세요")
        self._requestProducts()

    def _hChargeResult(self, msg: dict):
        if not msg.get("success"):
            reason = msg.get("reason", "")
            text = {
                "noCard": "카드를 인식하지 못했습니다. 다시 태그해주세요",
                "cardMismatch": "처음 태그한 카드와 다릅니다. 같은 카드로 충전해주세요",
                "cardTimeout": "카드 태그 시간이 초과되었습니다. 다시 시도해주세요",
                "readerBusy": "잠시 후 다시 시도해주세요",
                "cardReadError": "카드를 읽지 못했습니다. 다시 시도해주세요",
                "cardWriteError": "충전에 실패했습니다. 다시 시도해주세요",
                "invalidAmount": "충전 금액을 확인해주세요",
                "balanceLimit": "카드에 담을 수 있는 금액을 넘었습니다",
            }.get(reason, f"충전 실패 ({reason})")
            self._chargeFailed(text)
            return
        charged = msg.get("amount", 0)
        self._chargePending = False
        self._setBalance(msg.get("balance"))
        self._amountEdit.setText("")
        self._chargeBtn.setText("충전하기")
        self._refreshChargeTotal()
        self._chargeHint.setStyleSheet(f"color:{COL_OK};font-size:17px;")
        self._chargeHint.setText(
            f"{charged:,}원 충전 완료 · 잔액 {msg.get('balance', 0):,}원")

    def _hSlotData(self, msg: dict):
        self._slots = {int(state["slot"]): state
                       for state in msg.get("slots", []) if state.get("slot") is not None}
        self._renderSlots()

    def _hSlotReleased(self, msg: dict):
        # ★ broadcast — 남의 주문이 찾아가져도 온다. 함 상태 표시는 원래 전체 공유라
        #   내 주문인지 거르지 않는다.
        slot = msg.get("slot")
        if slot is None:
            return
        self._slots[int(slot)] = {"slot": int(slot), "occupied": False, "orderId": None}
        self._renderSlots()

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
        self._cancelHome()
        self._setProgress("🧾", "결제가 완료되었습니다",
                          f"주문번호 {self._orderId} · 상품을 준비합니다")
        self._showPage(PAGE_PROGRESS, "주문 진행")

    def _hDispatchStatus(self, msg: dict):
        # ★ broadcast 라 다른 손님 주문도 온다. 내 것만 본다.
        if msg.get("orderId") != self._orderId:
            return
        state = msg.get("state")
        if state == OrderStatus.DISPATCHING:
            self._setProgress("📦", "상품을 준비하고 있습니다", "잠시만 기다려주세요")
        elif state == OrderStatus.DONE:
            # 손님이 그 자리에서 바로 꺼낸 경우에만 여기까지 온다.
            # (보통은 pickupReady 의 자동 복귀가 먼저 걸려서 이미 홈이다)
            self._setProgress("🙇", "이용해 주셔서 감사합니다", "")
            self._scheduleHome(DONE_RESET_MS, "")
        elif state == OrderStatus.ERROR:
            # 실패 화면을 계속 띄워두면 키오스크가 통째로 멈춘다 — 오래 보여주되
            # 결국은 놓아준다. 물건/환불은 직원이 처리한다.
            self._setProgress("⚠️", "출고 중 문제가 발생했습니다",
                              "직원을 호출해 주세요", COL_DANGER)
            self._scheduleHome(ERROR_RESET_MS, "직원을 호출해 주세요")

    def _hPickupReady(self, msg: dict):
        # ★ 출고가 끝나 물건이 함에 놓였다. 여기서 화면을 놓아준다 —
        #   손님이 실제로 꺼낼 때까지 붙잡고 있으면 픽업박스가 3개여도
        #   다음 손님이 주문을 시작할 수 없다.
        slot = msg.get("slot")
        if slot is not None:
            # ★ 필터보다 먼저 — 남의 주문이 함에 놓인 것도 홈 하단에 떠야 한다
            self._slots[int(slot)] = {"slot": int(slot), "occupied": True,
                                      "orderId": msg.get("orderId")}
            self._renderSlots()
        if msg.get("orderId") != self._orderId:
            return
        self._setProgress("✅", f"{slot}번 함에서 찾아가세요", "", COL_OK)
        self._scheduleHome(PICKUP_RESET_MS, "물건을 꺼내주세요")

    def _hAlert(self, msg: dict):
        # ★ broadcast 라 다른 손님 주문 알림도 온다. 내 것만 본다.
        if msg.get("orderId") != self._orderId:
            return
        self._cancelHome()
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
