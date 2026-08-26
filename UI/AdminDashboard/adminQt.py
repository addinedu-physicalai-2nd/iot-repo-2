"""
adminQt.py — SmartMart 무인매장 관리자 대시보드 (PyQt6)

★ 이 파일은 화면만 그린다. 네트워크는 qtService.QtService 가 전담한다.
  포트 번호도 소켓도 여기 없다.

좌측 사이드 메뉴 + 정보 패널 + 하단 상태바.
네트워크는 qtService.QtService 가 전담한다(제어 TCP :9000 + 영상 UDP).
더미 데이터는 없다 — 모든 패널은 서버 응답과 push 로 채워진다.

패널별 데이터 출처:
  - 실시간 주문 현황   (SR-23) getAllOrders → allOrdersData + dispatchStatus push
  - 상품별 재고        (SR-15) getProducts   → productList
  - 픽업 슬롯 상태     (SR-11) pickupReady / slotReleased push
  - 컨베이어 상태      (SR-08) dispatchStatus 로부터 유도(출고중 주문 유무)
  - 화재·이상 알림     (SR-30) alert push + 이상감지 상태 + 서버 연결 끊김
  - 서버 연결 상태     (SR-07) QtService.connected / disconnected
  - 영상 모니터링      (SR-25) QtService.watchCamera — UDP 로 JPEG 청크 수신

화면 구성:
  사이드바 메뉴로 QStackedWidget 을 전환한다. '영상 모니터링' 을 볼 때만
  영상 연결을 열고, 떠나면 끊는다(안 보는 영상을 받지 않는다).

실행:  python adminQt.py [--host 127.0.0.1] [--port 9000]

갱신 방식:
  push 가 오면 즉시 반영하고, 서버가 push 하지 않는 값(재고 등)을 위해
  10초 주기 폴링을 안전망으로 함께 돌린다.

필요:  pip install PyQt6
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_UI_DIR = Path(__file__).resolve().parent.parent      # UI/  (theme.py, qtService.py 가 여기 있음)
_REPO_ROOT = _UI_DIR.parent                             # 저장소 루트 (Library/ 가 여기 있음)
sys.path.insert(0, str(_UI_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QListWidget, QListWidgetItem, QStackedWidget, QSizePolicy,
    QAbstractItemView, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QPixmap

from Library.protocol import OrderStatus
from UI.theme import (
    COL_BG, COL_PANEL, COL_PANEL_HDR, COL_SIDE, COL_SIDE_SEL,
    COL_TEXT, COL_SUBTLE, COL_LINE, COL_OK, COL_WARN, COL_DANGER,
)
from qtService import QtService


# 주문 상태별 표시색 (protocol.OrderStatus 와 1:1)
STATUS_COLOR = {
    OrderStatus.PENDING:      COL_SUBTLE,
    OrderStatus.PAID:         COL_TEXT,
    OrderStatus.DISPATCHING:  COL_WARN,
    OrderStatus.PICKUP_READY: COL_OK,
    OrderStatus.DONE:         COL_SUBTLE,
    OrderStatus.ERROR:        COL_DANGER,
}

SLOT_COUNT      = 3        # 픽업 슬롯 개수 (하드웨어 구성)
STOCK_CAP       = 20       # 상품 1칸 최대 적재량 (서버가 capacity 를 주면 그 값 사용)
MAX_ORDER_ROWS  = 20       # 주문 표에 보여줄 최근 건수
MAX_ALERTS      = 100      # 알림 목록 보관 개수
POLL_MS         = 10_000   # 폴링 안전망 주기
RESYNC_MS       = 400      # push 폭주 시 재조회 디바운스

# 카메라: 서버 camPorts 와 같은 이름을 쓴다 (centralControl.py 참조)
CAMERAS = [("checkout", "계산대"), ("dispensing", "출고구")]
VIDEO_FPS = 12             # 서버에 요청할 프레임률

MAX_LOG_ROWS = 300         # 통신 로그 보관 줄 수
LOG_DIRS = {               # commLog 의 dir -> (표시, 색)
    "toBoard":   ("→ 보드", COL_WARN),
    "fromBoard": ("← 보드", COL_OK),
    "toQt":      ("→ Qt",  COL_SUBTLE),
}


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    """제목 헤더가 달린 패널 프레임을 만든다. (프레임, 본문레이아웃) 반환"""
    frame = QFrame()
    # QLabel 이 QFrame 상속이라 QFrame{...} 로 쓰면 자식 라벨까지 테두리가 생긴다.
    frame.setObjectName("panelFrame")
    frame.setStyleSheet(
        f"QFrame#panelFrame{{background:{COL_PANEL};"
        f"border:1px solid {COL_LINE};border-radius:10px;}}")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    hdr = QLabel(title)
    hdr.setStyleSheet(
        f"background:{COL_PANEL_HDR};color:{COL_TEXT};font-weight:600;"
        f"padding:10px 14px;border-top-left-radius:10px;"
        f"border-top-right-radius:10px;")
    hdr.setFont(QFont("Malgun Gothic", 11, QFont.Weight.DemiBold))
    outer.addWidget(hdr)

    body = QVBoxLayout()
    body.setContentsMargins(14, 12, 14, 14)
    body.setSpacing(8)
    outer.addLayout(body)
    return frame, body


def clearLayout(lay):
    """레이아웃 안의 위젯을 전부 걷어낸다(패널 다시 그릴 때)."""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def hintLabel(text: str) -> QLabel:
    """데이터가 아직 없을 때 자리를 채우는 안내 문구"""
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(f"color:{COL_SUBTLE};font-size:12px;")
    return lbl


class VideoView(QLabel):
    """JPEG 한 장씩 받아 비율 유지하며 그리는 영상 위젯.

    마지막 원본을 들고 있다가 위젯 크기가 바뀌면 다시 스케일한다.
    """

    def __init__(self, placeholder: str = "신호 없음"):
        super().__init__()
        self._pix: QPixmap | None = None
        self._placeholder = placeholder
        self.setMinimumSize(240, 180)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            f"background:#000;color:{COL_SUBTLE};border:1px solid {COL_LINE};"
            "border-radius:6px;font-size:12px;")
        self.setText(placeholder)

    def setJpeg(self, data: bytes) -> bool:
        pix = QPixmap()
        if not pix.loadFromData(data, "JPG"):
            return False
        self._pix = pix
        self._rescale()
        return True

    def clearVideo(self):
        self._pix = None
        self.setPixmap(QPixmap())      # 픽스맵 비우고 안내문구 복귀
        self.setText(self._placeholder)

    def _rescale(self):
        if self._pix is None:
            return
        self.setPixmap(self._pix.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        self._rescale()
        super().resizeEvent(event)


class AdminDashboard(QMainWindow):
    def __init__(self, host: str = "192.168.0.225", port: int = 9000):
        super().__init__()
        self.setWindowTitle("SmartMart — 무인매장 관리 시스템")
        self.resize(1080, 720)
        self.setStyleSheet(f"background:{COL_BG};color:{COL_TEXT};")

        # ── 서버에서 받은 데이터 = 모든 패널의 단일 출처 ─────────
        self._orders: dict[int, dict] = {}                       # orderId -> 주문 dict
        self._products: list[dict] = []                          # 상품/재고
        self._slots: dict[int, int | None] = {n: None for n in range(1, SLOT_COUNT + 1)}
        self._hasAlert = False
        # 표 위젯은 페이지를 만들 때 채워진다. 페이지 0 을 만드는 도중
        # 아직 없는 페이지 2 의 표를 건드릴 수 있어 미리 비워둔다.
        self._ordersTable = None
        self._orderListTable = None
        self._cams: dict[str, dict] = {}      # camId -> {view, status, count}

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._buildSidebar())
        outer.addWidget(self._buildMain(), 1)

        # 1초마다 시계 갱신
        self._clockTimer = QTimer(self)
        self._clockTimer.timeout.connect(self._updateClock)
        self._clockTimer.start(1000)
        self._updateClock()

        # 영상 실측 fps 표시용
        self._videoFpsTimer = QTimer(self)
        self._videoFpsTimer.timeout.connect(self._tickVideoFps)
        self._videoFpsTimer.start(1000)

        # ── 서버 연결 — 전송 방식은 QtService 가 감춘다 ──────────
        self._net = QtService(host, port, parent=self)
        self._net.connected.connect(self._onConnected)
        self._net.disconnected.connect(self._onDisconnected)
        self._net.message.connect(self._onMessage)
        self._net.frame.connect(self._onFrame)
        self._net.cameraState.connect(self._onCamState)

        # push 가 몰려도 재조회는 한 번만 나가게 묶는다
        self._resyncTimer = QTimer(self)
        self._resyncTimer.setSingleShot(True)
        self._resyncTimer.setInterval(RESYNC_MS)
        self._resyncTimer.timeout.connect(self._requestRefresh)

        # 서버가 push 하지 않는 값(재고)을 위한 폴링 안전망
        self._pollTimer = QTimer(self)
        self._pollTimer.timeout.connect(self._requestRefresh)
        self._pollTimer.start(POLL_MS)

        self._setServerStatus(False)
        self._net.start()

    # ── 좌측 사이드 메뉴 ─────────────────────────────────────────
    def _buildSidebar(self) -> QWidget:
        side = QWidget()
        side.setFixedWidth(190)
        side.setStyleSheet(f"background:{COL_SIDE};")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 18, 12, 18)
        lay.setSpacing(6)

        logo = QLabel("SmartMart")
        logo.setStyleSheet(f"color:{COL_TEXT};font-size:18px;font-weight:700;"
                           "padding:4px 8px 16px 8px;")
        lay.addWidget(logo)

        menus = ["대시보드", "주문 관리", "재고 관리", "회원 관리",
                 "영상 모니터링", "화재·환경"]
        self._menuBtns = []
        for i, name in enumerate(menus):
            btn = QPushButton(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            selected = (i == 0)
            btn.setStyleSheet(self._menuStyle(selected))
            btn.clicked.connect(lambda _, b=btn: self._selectMenu(b))
            lay.addWidget(btn)
            self._menuBtns.append(btn)

        lay.addStretch(1)
        return side

    def _menuStyle(self, selected: bool) -> str:
        if selected:
            return (f"QPushButton{{background:{COL_SIDE_SEL};color:white;"
                    "text-align:left;padding:11px 14px;border:none;"
                    "border-radius:8px;font-weight:600;}")
        return (f"QPushButton{{background:transparent;color:{COL_SUBTLE};"
                "text-align:left;padding:11px 14px;border:none;"
                "border-radius:8px;}"
                f"QPushButton:hover{{background:{COL_PANEL};color:{COL_TEXT};}}")

    def _selectMenu(self, clickedBtn):
        for b in self._menuBtns:
            b.setStyleSheet(self._menuStyle(b is clickedBtn))
        name = clickedBtn.text()
        # 아직 화면이 없는 메뉴는 대시보드를 유지한다. TODO: 나머지 화면 추가
        page = {"영상 모니터링": 1, "주문 관리": 2}.get(name, 0)
        self._showPage(page, name if page else "대시보드")

    def _showPage(self, index: int, title: str):
        self._stack.setCurrentIndex(index)
        self._title.setText(title)
        # 영상은 보고 있을 때만 받는다(안 보는 화면 때문에 대역폭 쓰지 않게)
        for camId, cam in self._cams.items():
            if index == 1:
                self._net.watchCamera(camId, VIDEO_FPS)
            else:
                self._net.unwatchCamera(camId)
                cam["view"].clearVideo()
                cam["status"].setText("연결 대기")

    # ── 우측 메인 영역 ───────────────────────────────────────────
    def _buildMain(self) -> QWidget:
        main = QWidget()
        lay = QVBoxLayout(main)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(14)

        # 상단 바: 제목 + 시계
        topbar = QHBoxLayout()
        title = QLabel("대시보드")
        title.setStyleSheet(f"color:{COL_TEXT};font-size:20px;font-weight:700;")
        self._title = title
        self._clock = QLabel("")
        self._clock.setStyleSheet(f"color:{COL_SUBTLE};font-size:14px;")
        topbar.addWidget(title)
        topbar.addStretch(1)
        topbar.addWidget(self._clock)
        lay.addLayout(topbar)

        # 화면 전환: 0=대시보드, 1=영상 모니터링
        self._stack = QStackedWidget()
        self._stack.addWidget(self._pageDashboard())
        self._stack.addWidget(self._pageCameras())
        self._stack.addWidget(self._pageOrderAdmin())
        lay.addWidget(self._stack, 1)

        # 하단 상태바
        lay.addWidget(self._statusbar())
        return main

    # ── 화면 1: 대시보드 ─────────────────────────────────────────
    def _pageDashboard(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        # 패널 그리드 (2열)
        grid = QGridLayout()
        grid.setSpacing(14)
        grid.addWidget(self._panelOrders(),    0, 0)
        grid.addWidget(self._panelStock(),     0, 1)
        grid.addWidget(self._panelAlerts(),    1, 0)
        rightCol = QVBoxLayout()
        rightCol.setSpacing(14)
        rightCol.addWidget(self._panelSlots())
        rightCol.addWidget(self._panelConveyor())
        rcWrap = QWidget()
        rcWrap.setLayout(rightCol)
        grid.addWidget(rcWrap, 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        lay.addLayout(grid, 1)
        return page

    # ── 화면 2: 영상 모니터링 (SR-25) ────────────────────────────
    def _pageCameras(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        for camId, label in CAMERAS:
            lay.addWidget(self._panelCamera(camId, label), 1)
        return page

    # ── 화면 3: 주문 관리 (주문 목록 + 실시간 통신 로그) ────────
    def _pageOrderAdmin(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(self._panelOrderList(), 2)
        lay.addWidget(self._panelCommLog(), 3)
        return page

    def _panelOrderList(self) -> QFrame:
        frame, body = panel("주문 목록")

        bar = QHBoxLayout()
        bar.addStretch(1)
        resetBtn = QPushButton("테스트 데이터 초기화")
        resetBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        resetBtn.setStyleSheet(
            f"QPushButton{{background:{COL_PANEL_HDR};color:{COL_DANGER};"
            f"border:1px solid {COL_DANGER};border-radius:6px;padding:5px 14px;}}"
            f"QPushButton:hover{{background:{COL_DANGER};color:white;}}")
        resetBtn.clicked.connect(self._confirmResetTestData)
        bar.addWidget(resetBtn)
        body.addLayout(bar)

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["주문번호", "카드", "상태", "슬롯", "상품"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(
            f"QTableWidget{{background:{COL_PANEL};color:{COL_TEXT};"
            f"gridline-color:{COL_LINE};border:none;}}"
            f"QHeaderView::section{{background:{COL_PANEL_HDR};color:{COL_SUBTLE};"
            f"border:none;padding:6px;}}")
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._orderListTable = table
        body.addWidget(table)
        return frame

    def _panelCommLog(self) -> QFrame:
        frame, body = panel("실시간 통신 로그")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._logPauseBtn = QPushButton("일시정지")
        self._logClearBtn = QPushButton("지우기")
        for btn in (self._logPauseBtn, self._logClearBtn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{background:{COL_PANEL_HDR};color:{COL_TEXT};"
                f"border:1px solid {COL_LINE};border-radius:6px;padding:5px 14px;}}"
                f"QPushButton:hover{{background:{COL_LINE};}}")
        self._logPaused = False
        self._logPauseBtn.clicked.connect(self._toggleLogPause)
        self._logClearBtn.clicked.connect(lambda: self._logTable.setRowCount(0))
        self._logCount = QLabel("0줄")
        self._logCount.setStyleSheet(f"color:{COL_SUBTLE};font-size:12px;")
        bar.addWidget(self._logCount)
        bar.addStretch(1)
        bar.addWidget(self._logPauseBtn)
        bar.addWidget(self._logClearBtn)
        body.addLayout(bar)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["시각", "방향", "상대", "내용"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setStyleSheet(
            f"QTableWidget{{background:{COL_BG};color:{COL_TEXT};"
            f"gridline-color:{COL_LINE};border:1px solid {COL_LINE};"
            "border-radius:6px;font-family:monospace;font-size:12px;}"
            f"QHeaderView::section{{background:{COL_PANEL_HDR};color:{COL_SUBTLE};"
            f"border:none;padding:6px;font-family:'Malgun Gothic';}}")
        head = table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._logTable = table
        body.addWidget(table)
        return frame

    def _toggleLogPause(self):
        self._logPaused = not self._logPaused
        self._logPauseBtn.setText("이어보기" if self._logPaused else "일시정지")

    # ── 통신 로그 수신 ───────────────────────────────────────────
    def _hCommLog(self, msg: dict):
        """서버가 중계한 오간 메시지 한 건.

        일시정지 중에는 쌓지 않는다. 프로토콜을 눈으로 좇는 중에
        화면이 흘러가버리면 읽을 수가 없다.
        """
        if self._logPaused:
            return
        label, color = LOG_DIRS.get(msg.get("dir", ""), (msg.get("dir", "?"), COL_TEXT))
        payload = msg.get("payload", {})
        stamp = datetime.fromtimestamp(msg.get("ts", 0)).strftime("%H:%M:%S.%f")[:-3]
        text = json.dumps(payload, ensure_ascii=False)
        if not msg.get("ok", True):
            label += " ✗"
            color = COL_DANGER

        table = self._logTable
        row = table.rowCount()
        table.insertRow(row)
        for col, value in enumerate((stamp, label, str(msg.get("peer", "")), text)):
            item = QTableWidgetItem(value)
            item.setForeground(QColor(color if col == 1 else COL_TEXT))
            table.setItem(row, col, item)

        while table.rowCount() > MAX_LOG_ROWS:
            table.removeRow(0)
        table.scrollToBottom()          # 최신 줄이 항상 보이게
        self._logCount.setText(f"{table.rowCount()}줄")

    def _refreshOrderList(self):
        """주문 관리 탭의 주문 목록. 대시보드 표보다 상품 내역을 더 보여준다."""
        table = self._orderListTable
        if table is None:
            return
        rows = sorted(self._orders.values(), key=lambda o: o.get("id", 0), reverse=True)
        table.setRowCount(len(rows))
        for r, o in enumerate(rows):
            slot = o.get("assignedSlot")
            status = o.get("status", "-")
            items = o.get("items") or []
            itemText = ", ".join(f"#{it.get('productId')}×{it.get('qty')}"
                                 for it in items) if items else "-"
            values = [str(o.get("id", "-")), self._cardLabel(o.get("cardUid")),
                      status, str(slot) if slot else "-", itemText]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c != 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 2:
                    item.setForeground(QColor(STATUS_COLOR.get(status, COL_TEXT)))
                table.setItem(r, c, item)

    def _panelCamera(self, camId: str, label: str) -> QFrame:
        frame, body = panel(f"{label} 카메라")
        view = VideoView("영상 없음 — 카메라 신호 대기 중")
        status = QLabel("연결 대기")
        status.setStyleSheet(f"color:{COL_SUBTLE};font-size:12px;")
        body.addWidget(view, 1)
        body.addWidget(status)

        # 영상 수신은 QtService 가 맡는다. 여기는 그릴 위젯만 등록한다.
        self._cams[camId] = {"view": view, "status": status, "count": 0}
        return frame

    # ── 패널: 실시간 주문 현황 (SR-23) ───────────────────────────
    def _panelOrders(self) -> QFrame:
        frame, body = panel("실시간 주문 현황")
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["주문번호", "카드", "상태", "슬롯"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(
            f"QTableWidget{{background:{COL_PANEL};color:{COL_TEXT};"
            f"gridline-color:{COL_LINE};border:none;}}"
            f"QHeaderView::section{{background:{COL_PANEL_HDR};color:{COL_SUBTLE};"
            f"border:none;padding:6px;}}")
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._ordersTable = table
        body.addWidget(table)
        self._refreshOrders()
        return frame

    def _refreshOrders(self):
        """self._orders 를 화면에 반영한다.

        대시보드 표와 주문 관리 탭 목록을 여기서 함께 갱신한다.
        호출부마다 둘을 챙기면 반드시 한쪽을 빠뜨린다.
        """
        self._refreshOrderList()
        self._refreshOrdersTable()

    def _refreshOrdersTable(self):
        """대시보드의 '실시간 주문 현황' 표"""
        t = self._ordersTable
        if t is None:
            return
        t.clearSpans()
        rows = sorted(self._orders.values(),
                      key=lambda o: o.get("id", 0), reverse=True)[:MAX_ORDER_ROWS]

        if not rows:
            t.setRowCount(1)
            msg = ("주문 없음" if self._clientReady()
                   else "서버 응답 대기 중…")
            item = QTableWidgetItem(msg)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setForeground(QColor(COL_SUBTLE))
            t.setItem(0, 0, item)
            t.setSpan(0, 0, 1, 4)
            return

        t.setRowCount(len(rows))
        for r, o in enumerate(rows):
            slot = o.get("assignedSlot")
            status = o.get("status", "-")
            vals = [str(o.get("id", "-")),
                    self._cardLabel(o.get("cardUid")),
                    status,
                    str(slot) if slot else "-"]
            for c, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 2:
                    item.setForeground(QColor(STATUS_COLOR.get(status, COL_TEXT)))
                t.setItem(r, c, item)

    def _cardLabel(self, cardUid) -> str:
        """카드 UID(hex) 짧게 표시. 결제 태그 전이면 아직 없음."""
        if not cardUid:
            return "-"
        return cardUid.upper()

    # ── 패널: 상품별 재고 (SR-15) ────────────────────────────────
    def _panelStock(self) -> QFrame:
        frame, body = panel("상품별 재고")
        self._stockRow = QHBoxLayout()
        self._stockRow.setSpacing(12)
        body.addLayout(self._stockRow)
        self._refreshStock()
        return frame

    def _refreshStock(self):
        """productList 응답을 게이지로 다시 그린다"""
        clearLayout(self._stockRow)
        if not self._products:
            self._stockRow.addWidget(hintLabel("상품 정보 수신 대기 중…"))
            return
        for p in self._products:
            self._stockRow.addWidget(self._stockGauge(
                p.get("name", "?"),
                int(p.get("stock", 0)),
                int(p.get("capacity", STOCK_CAP))))

    def _stockGauge(self, name: str, cur: int, cap: int) -> QWidget:
        pct = max(0, min(100, int(cur / cap * 100))) if cap else 0
        low = cur <= 5
        col = COL_DANGER if low else (COL_WARN if pct < 40 else COL_OK)

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        nm = QLabel(name)
        nm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nm.setStyleSheet(f"background:transparent;color:{COL_TEXT};font-weight:600;")
        v.addWidget(nm)

        # 세로 게이지 (배경 바 + 채움 바)
        barBg = QFrame()
        barBg.setFixedHeight(120)
        barBg.setStyleSheet(
            f"background:{COL_BG};border:1px solid {COL_LINE};border-radius:6px;")
        bgLay = QVBoxLayout(barBg)
        bgLay.setContentsMargins(6, 6, 6, 6)
        bgLay.addStretch(max(0, 100 - pct))
        fill = QFrame()
        fill.setObjectName("gaugeFill")
        fill.setStyleSheet(f"QFrame#gaugeFill{{background:{col};border-radius:4px;}}")
        fill.setMinimumHeight(max(4, int(108 * pct / 100)))
        bgLay.addWidget(fill)
        v.addWidget(barBg)

        info = QLabel(f"{cur} / {cap}")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:12px;")
        v.addWidget(info)

        badge = QLabel("재고 부족" if low else "정상")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background:{col};color:#0F1115;border-radius:5px;"
            "padding:3px 0;font-size:11px;font-weight:600;")
        v.addWidget(badge)
        return w

    # ── 패널: 화재·이상 알림 (SR-30) ─────────────────────────────
    def _panelAlerts(self) -> QFrame:
        frame, body = panel("화재·이상 알림")
        lst = QListWidget()
        lst.setStyleSheet(
            f"QListWidget{{background:{COL_PANEL};color:{COL_TEXT};border:none;}}"
            f"QListWidget::item{{padding:8px;border-bottom:1px solid {COL_LINE};}}")
        lst.addItem("· 알림 없음 — 정상 운영 중")
        self._alertsList = lst
        body.addWidget(lst)
        return frame

    def _addAlert(self, text: str, color: str = COL_WARN):
        """alert push / 이상감지 / 연결 끊김을 최신순으로 쌓는다"""
        if not self._hasAlert:
            self._alertsList.clear()      # "알림 없음" 안내 제거
            self._hasAlert = True
        stamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"· [{stamp}] {text}")
        item.setForeground(QColor(color))
        self._alertsList.insertItem(0, item)
        while self._alertsList.count() > MAX_ALERTS:
            self._alertsList.takeItem(self._alertsList.count() - 1)

    # ── 패널: 픽업 슬롯 상태 (SR-11) ─────────────────────────────
    def _panelSlots(self) -> QFrame:
        frame, body = panel("픽업 슬롯 상태")
        self._slotRow = QHBoxLayout()
        self._slotRow.setSpacing(10)
        body.addLayout(self._slotRow)
        self._refreshSlots()
        return frame

    def _refreshSlots(self):
        """pickupReady / slotReleased push 로 갱신된 self._slots 를 다시 그린다"""
        clearLayout(self._slotRow)
        for num in sorted(self._slots):
            self._slotRow.addWidget(self._slotBox(num, self._slots[num]))

    def _slotBox(self, num: int, orderId: int | None) -> QWidget:
        occupied = orderId is not None
        col = COL_OK if occupied else COL_SUBTLE
        state = f"대기중 · #{orderId}" if occupied else "비어있음"

        w = QFrame()
        w.setObjectName("slotBox")
        w.setStyleSheet(
            f"QFrame#slotBox{{background:{COL_BG};"
            f"border:1px solid {col};border-radius:8px;}}")
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 10, 8, 10)
        v.setSpacing(4)
        n = QLabel(f"슬롯 {num}")
        n.setAlignment(Qt.AlignmentFlag.AlignCenter)
        n.setStyleSheet(f"background:transparent;color:{COL_TEXT};font-weight:600;")
        s = QLabel(state)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.setStyleSheet(f"background:transparent;color:{col};font-size:12px;")
        v.addWidget(n)
        v.addWidget(s)
        return w

    # ── 패널: 컨베이어 상태 (SR-08) ──────────────────────────────
    def _panelConveyor(self) -> QFrame:
        frame, body = panel("컨베이어 상태")
        row = QHBoxLayout()
        lbl = QLabel("컨베이어")
        lbl.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};")
        state = QLabel("정지")
        self._conveyorState = state
        row.addWidget(lbl)
        row.addStretch(1)
        row.addWidget(state)
        body.addLayout(row)
        self._refreshConveyor()
        return frame

    def _refreshConveyor(self):
        """서버가 컨베이어 상태를 따로 push 하지 않으므로 '출고중' 주문 유무로 유도한다.
        (서버에 beltStatus push 가 생기면 그걸 그대로 쓰도록 바꾸면 된다.)"""
        running = any(o.get("status") == OrderStatus.DISPATCHING
                      for o in self._orders.values())
        col = COL_WARN if running else COL_TEXT
        self._conveyorState.setText("가동중" if running else "정지")
        self._conveyorState.setStyleSheet(
            f"background:{COL_PANEL_HDR};color:{col};padding:5px 14px;"
            "border-radius:6px;font-weight:600;")

    # ── 하단 상태바 (SR-07 서버 연결 상태) ───────────────────────
    def _statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(30)
        bar.setStyleSheet(
            f"QFrame#statusBar{{background:{COL_PANEL};border-radius:6px;}}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        dot = QLabel("●")
        txt = QLabel("서버 연결 상태: 연결 중…")
        txt.setStyleSheet(f"background:transparent;color:{COL_SUBTLE};font-size:12px;")
        self._serverDot = dot
        self._serverStatus = txt
        lay.addWidget(dot)
        lay.addWidget(txt)
        lay.addStretch(1)
        return bar

    def _setServerStatus(self, connected: bool):
        col = COL_OK if connected else COL_DANGER
        where = f"{self._net.host}:{self._net.port}" if hasattr(self, "_net") else ""
        text = (f"서버 연결 상태: 연결됨 ({where})" if connected
                else f"서버 연결 상태: 끊김 — 재연결 시도 중 ({where})")
        self._serverDot.setStyleSheet(f"background:transparent;color:{col};")
        self._serverStatus.setText(text)

    def _clientReady(self) -> bool:
        return hasattr(self, "_net") and self._net.isConnected()

    def _updateClock(self):
        self._clock.setText(datetime.now().strftime("%Y.%m.%d  %H:%M:%S"))

    # ── 영상 (SR-25) ─────────────────────────────────────────────
    def _onFrame(self, camId: str, jpeg: bytes):
        cam = self._cams.get(camId)
        if cam is None:
            return
        if cam["view"].setJpeg(jpeg):
            cam["count"] += 1
        else:
            cam["status"].setText("디코딩 실패 — JPEG 형식 확인 필요")

    def _onCamState(self, camId: str, connected: bool):
        cam = self._cams.get(camId)
        if cam is None:
            return
        if not connected:
            cam["count"] = 0
            cam["view"].clearVideo()
        cam["status"].setText("수신 중" if connected else "서버 영상 연결 끊김 — 재시도 중")

    def _tickVideoFps(self):
        """1초마다 실측 fps 표시(영상 화면일 때만)"""
        if self._stack.currentIndex() != 1:
            return
        for camId, cam in self._cams.items():
            if not self._net.isWatching(camId):
                continue
            done, lost = self._net.cameraStats(camId)
            text = f"수신 중 · {done} fps"
            if lost:
                text += f" · 유실 {lost}"     # 무선 구간에서 조각이 빠진 프레임
            cam["status"].setText(text)
            cam["count"] = 0

    # ── 서버 요청 ────────────────────────────────────────────────
    def _requestRefresh(self):
        """주문/재고 재조회 (폴링 안전망 + push 뒤 재동기화)"""
        self._net.send({"cmd": "getAllOrders"})
        self._net.send({"cmd": "getProducts"})

    def _scheduleResync(self):
        self._resyncTimer.start()   # 이미 돌고 있으면 타이머가 리셋됨(디바운스)

    def _confirmResetTestData(self):
        """개발/테스트 중 쌓인 주문·재고를 지운다. 실수로 누르면 안 되니 확인창을 띄운다."""
        ok = QMessageBox.question(
            self, "테스트 데이터 초기화",
            "모든 주문 내역을 지우고 재고를 초기값으로 되돌립니다.\n"
            "회원/상품 목록은 그대로 유지됩니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ok == QMessageBox.StandardButton.Yes:
            self._net.send({"cmd": "resetTestData"})

    # ── 연결 상태 ────────────────────────────────────────────────
    def _onConnected(self):
        self._setServerStatus(True)
        self._requestRefresh()

    def _onDisconnected(self):
        self._setServerStatus(False)
        self._addAlert("서버 연결이 끊겼습니다", COL_DANGER)
        self._refreshOrders()

    # ── 서버 메시지 라우팅 (응답 + push) ─────────────────────────
    def _onMessage(self, msg: dict):
        handler = {
            # 요청 응답
            "allOrdersData":     self._hAllOrders,
            "productList":        self._hProductList,
            "updateStockResult": self._hUpdateStockResult,
            "resetTestDataResult": self._hResetTestDataResult,
            # push (broadcast)
            "dispatchStatus":     self._hDispatchStatus,
            "pickupReady":        self._hPickupReady,
            "slotReleased":       self._hSlotReleased,
            "alert":               self._hAlert,
            "commLog":             self._hCommLog,
            "error":               self._hError,
        }.get(msg.get("cmd"))
        if handler:
            handler(msg)

    # ── 응답 핸들러 ──────────────────────────────────────────────
    def _hAllOrders(self, msg: dict):
        self._orders = {o["id"]: dict(o) for o in msg.get("orders", []) if "id" in o}
        self._rebuildSlotsFromOrders()
        self._refreshOrders()
        self._refreshSlots()
        self._refreshConveyor()

    def _hProductList(self, msg: dict):
        self._products = msg.get("items", [])
        self._refreshStock()

    def _hUpdateStockResult(self, msg: dict):
        if msg.get("success"):
            self._scheduleResync()
        else:
            self._addAlert("재고 수정 실패", COL_DANGER)

    def _hResetTestDataResult(self, msg: dict):
        if msg.get("success"):
            self._orders.clear()
            self._addAlert("테스트 데이터를 초기화했습니다", COL_OK)
            self._requestRefresh()
        else:
            self._addAlert("테스트 데이터 초기화 실패", COL_DANGER)

    # ── push 핸들러 ──────────────────────────────────────────────
    def _hDispatchStatus(self, msg: dict):
        orderId, state = msg.get("orderId"), msg.get("state")
        if orderId is None:
            return

        order = self._orders.get(orderId)
        if order is None:
            # 표에 없던 주문 → 일단 상태만 잡아두고 나머지는 재조회로 채운다
            order = {"id": orderId, "cardUid": None, "assignedSlot": None}
            self._orders[orderId] = order
            self._scheduleResync()
        order["status"] = state

        if state == OrderStatus.DISPATCHING:
            self._scheduleResync()          # 출고로 재고가 줄었으니 다시 조회
        elif state == OrderStatus.DONE:
            self._freeSlotOf(orderId)
        elif state == OrderStatus.ERROR:
            self._addAlert(f"주문 {orderId} 이상 감지", COL_DANGER)

        self._refreshOrders()
        self._refreshSlots()
        self._refreshConveyor()

    def _hPickupReady(self, msg: dict):
        orderId, slot = msg.get("orderId"), msg.get("slot")
        order = self._orders.setdefault(
            orderId, {"id": orderId, "cardUid": None, "status": OrderStatus.PICKUP_READY})
        order["assignedSlot"] = slot
        if slot in self._slots:
            self._slots[slot] = orderId
        self._refreshOrders()
        self._refreshSlots()

    def _hSlotReleased(self, msg: dict):
        slot = msg.get("slot")
        if slot in self._slots:
            self._slots[slot] = None
        for o in self._orders.values():
            if o.get("assignedSlot") == slot:
                o["assignedSlot"] = None
        self._refreshOrders()
        self._refreshSlots()

    def _hAlert(self, msg: dict):
        """화재/환경 이상 push (서버에 구현되면 그대로 받는다)"""
        level = msg.get("level", "warn")
        col = {"info": COL_OK, "warn": COL_WARN}.get(level, COL_DANGER)
        self._addAlert(msg.get("message") or msg.get("reason") or "이상 감지", col)

    def _hError(self, msg: dict):
        self._addAlert(f"서버 오류: {msg.get('reason', '알 수 없음')}", COL_DANGER)

    # ── 슬롯 헬퍼 ────────────────────────────────────────────────
    def _rebuildSlotsFromOrders(self):
        self._slots = {n: None for n in self._slots}
        for o in self._orders.values():
            slot = o.get("assignedSlot")
            if slot in self._slots and o.get("status") != OrderStatus.DONE:
                self._slots[slot] = o.get("id")

    def _freeSlotOf(self, orderId: int):
        order = self._orders.get(orderId, {})
        slot = order.get("assignedSlot")
        if slot in self._slots and self._slots[slot] == orderId:
            self._slots[slot] = None
        order["assignedSlot"] = None

    # ── 종료 ─────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._net.stop()
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description="SmartMart 관리자 대시보드")
    ap.add_argument("--host", default="192.168.0.225", help="centralControl 서버 주소")
    ap.add_argument("--port", type=int, default=9000, help="제어 TCP 포트 (기본 9000)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    win = AdminDashboard(args.host, args.port)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
