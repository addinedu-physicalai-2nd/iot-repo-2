"""
dbManager.py
SmartMart DB 접근 모듈 (MySQL, pymysql)

사용 전 준비:
    pip install pymysql python-dotenv

같은 폴더에 .env 파일 넣기 ( Git에 안올림)
"""

import os
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

_MODULE_DIR = Path(__file__).resolve().parent
load_dotenv(_MODULE_DIR / ".env")   # cwd 와 무관하게 항상 이 파일 옆의 .env 를 읽는다

_SCHEMA_PATH = _MODULE_DIR / "Schema.SQL"

# orders 테이블 컬럼(snake_case) -> 서버가 쓰는 camelCase 로 alias.
# mainService.Order.fromDbRow() 가 cardId/assignedSlot 키를 그대로 기대한다.
# card/member 를 조인해서 cardUid/memberName 도 같이 준다 — 관리자 화면 표시용.
_ORDER_SELECT = (
    "SELECT o.id, o.card_id AS cardId, c.uid AS cardUid, m.name AS memberName, "
    "o.status, o.assigned_slot AS assignedSlot, o.total_price AS totalPrice, "
    "o.created_at AS createdAt, o.paid_at AS paidAt, "
    "o.balance_before AS balanceBefore, o.balance_after AS balanceAfter "
    "FROM orders o "
    "LEFT JOIN card c ON o.card_id = c.id "
    "LEFT JOIN member m ON c.member_id = m.id "
)


class DBManager:
    """member / card / product / orders / order_item 에 대한 CRUD 모음.
    mainService에서 db = DBManager() 로 한 번만 만들어서 계속 재사용한다."""

    def __init__(self):
        """RDS(원격) 연결을 여기서 바로 맺어서 계속 재사용한다.

        호출마다 새로 열면 접속 핸드셰이크만으로 매번 1초 넘게 걸리는데,
        그 비용을 첫 요청이 아니라 서버 기동 시점(DBManager() 생성)에 미리
        치러둔다. ※ initDb() 로 스키마가 이미 만들어져 있어야 한다 — DB 자체가
        없는 상태에서 여기서 바로 접속하면 Unknown database 에러가 난다.
        """
        self.host = os.getenv("DB_HOST")
        self.port = int(os.getenv("DB_PORT", 3306))
        self.user = os.getenv("DB_USER")
        self.password = os.getenv("DB_PASSWORD")
        self.database = os.getenv("DB_NAME")
        self._conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )

    def _connect(self):
        """살아있는 연결을 반환한다. 끊겼으면 ping(reconnect=True) 가 다시 연다."""
        self._conn.ping(reconnect=True)
        return self._conn

    def initDb(self):
        """서버 기동 시 한 번 호출. Schema.SQL 을 그대로 실행해 DB/테이블이
        없으면 만든다(CREATE ... IF NOT EXISTS 라 이미 있으면 그냥 넘어간다).
        DB 자체가 없을 수 있어 database= 없이 별도 연결로 접속한다."""
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, charset="utf8mb4", autocommit=True,
        )
        try:
            sql = _SCHEMA_PATH.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                for statement in sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        cur.execute(statement)
        finally:
            conn.close()

    # ------------------------------------------------------------
    # product
    # ------------------------------------------------------------

    def getProducts(self):
        """상품 목록 전체."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM product ORDER BY id")
            return cur.fetchall()

    def updateStock(self, productId, newStock):
        """재고 직접 수정 (관리자용). 없는 상품이거나 음수면 False."""
        if newStock < 0:
            return False
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM product WHERE id = %s", (productId,))
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE product SET stock = %s WHERE id = %s",
                (newStock, productId),
            )
            return True

    # ------------------------------------------------------------
    # orders (핵심)
    # ------------------------------------------------------------

    def getCardByUid(self, uid):
        """카드 UID로 등록된 카드+회원 정보 조회. 등록 안 된 카드면 None."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.member_id AS memberId, c.uid, m.name AS memberName "
                "FROM card c JOIN member m ON c.member_id = m.id "
                "WHERE c.uid = %s",
                (uid,),
            )
            return cur.fetchone()

    def getCards(self):
        """등록된 카드 전체 (회원 정보 포함). 관리자 화면의 회원 관리 탭용."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.uid, c.created_at AS createdAt, "
                "m.id AS memberId, m.name AS memberName, m.contact "
                "FROM card c JOIN member m ON c.member_id = m.id "
                "ORDER BY c.id"
            )
            return cur.fetchall()

    def registerCard(self, uid, name, contact=None, memberId=None):
        """카드를 등록한다. 반환: (True, cardId) / 실패 시 (False, 사유)

        memberId 를 주면 그 회원에게 카드를 한 장 더 붙이고(schema 가 1:N 허용),
        안 주면 회원을 새로 만든다. uid 는 항상 소문자 hex 로 저장한다 —
        보드/DB/클라이언트에 대소문자가 섞여도 같은 카드로 인식되게.
        """
        uid = (uid or "").strip().lower()
        if not uid:
            return False, "noUid"
        if memberId is None and not (name or "").strip():
            return False, "noName"

        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM card WHERE uid = %s", (uid,))
            if cur.fetchone() is not None:
                return False, "duplicateCard"

            if memberId is None:
                cur.execute(
                    "INSERT INTO member (name, contact) VALUES (%s, %s)",
                    (name.strip(), (contact or "").strip() or None),
                )
                memberId = cur.lastrowid
            else:
                cur.execute("SELECT id FROM member WHERE id = %s", (memberId,))
                if cur.fetchone() is None:
                    return False, "noMember"

            cur.execute(
                "INSERT INTO card (member_id, uid) VALUES (%s, %s)",
                (memberId, uid),
            )
            return True, cur.lastrowid

    def createOrder(self, cardId, items):
        """
        주문 생성. items 예: [{"productId": 1, "qty": 2}, ...]
        cardId 는 상품 고르기 전에 이미 태그해서 알고 있는 값(getCardByUid 로 얻음)
        — 주문 생성 시점부터 카드가 비어있는 행이 안 생기게 처음부터 넣는다.
        재고를 먼저 전부 확인하고, 하나라도 부족하면 아무것도 안 바꾸고 거절.
        전부 통과해야 그때 재고 차감 + 주문 생성.
        반환: (True, orderId, totalPrice) / 실패 시 (False, None, None)
        """
        if not items:
            return False, None, None

        conn = self._connect()
        with conn.cursor() as cur:
            total = 0

            # 1단계: 재고 확인만 (아직 아무것도 안 바꿈)
            for item in items:
                cur.execute(
                    "SELECT price, stock FROM product WHERE id = %s",
                    (item["productId"],),
                )
                product = cur.fetchone()
                if product is None or product["stock"] < item["qty"]:
                    return False, None, None
                total += product["price"] * item["qty"]

            # 2단계: 전부 통과했으니 재고 차감
            for item in items:
                cur.execute(
                    "UPDATE product SET stock = stock - %s WHERE id = %s",
                    (item["qty"], item["productId"]),
                )

            # 3단계: 주문 헤더 생성
            cur.execute(
                "INSERT INTO orders (card_id, status, total_price) "
                "VALUES (%s, '대기', %s)",
                (cardId, total),
            )
            orderId = cur.lastrowid

            # 4단계: 주문 품목들 생성
            for item in items:
                cur.execute(
                    "INSERT INTO order_item (order_id, product_id, qty) "
                    "VALUES (%s, %s, %s)",
                    (orderId, item["productId"], item["qty"]),
                )

            return True, orderId, total

    def cancelOrder(self, orderId):
        """결제 실패(잔액부족 등)로 주문을 취소. createOrder 때 미리 깎은 재고를
        되돌리고 주문/품목 행을 지운다."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id, qty FROM order_item WHERE order_id = %s",
                (orderId,),
            )
            items = cur.fetchall()
            for item in items:
                cur.execute(
                    "UPDATE product SET stock = stock + %s WHERE id = %s",
                    (item["qty"], item["product_id"]),
                )
            cur.execute("DELETE FROM order_item WHERE order_id = %s", (orderId,))
            cur.execute("DELETE FROM orders WHERE id = %s", (orderId,))

    def restoreStock(self, items):
        """createOrder 가 미리 깎아둔 재고 중 실제로 안 나간 몫을 되돌린다.

        cancelOrder 와 달리 주문 행은 남긴다 — 출고 실패는 '없던 일' 이 아니라
        기록에 남아야 하는 사고라서(관리자 화면에 오류 주문으로 뜬다).
        items 예: [{"productId": 1, "qty": 2}, ...]
        """
        if not items:
            return
        conn = self._connect()
        with conn.cursor() as cur:
            for item in items:
                if item["qty"] <= 0:
                    continue
                cur.execute(
                    "UPDATE product SET stock = stock + %s WHERE id = %s",
                    (item["qty"], item["productId"]),
                )

    def confirmPayment(self, orderId, balanceBefore, balanceAfter):
        """결제 확정 표시 (paidAt/차감 전후 잔액 기록 + status).
        재고는 createOrder에서 이미 처리했음."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET paid_at = NOW(), status = '결제완료', "
                "balance_before = %s, balance_after = %s "
                "WHERE id = %s",
                (balanceBefore, balanceAfter, orderId),
            )

    def updateOrderStatus(self, orderId, status):
        """주문 상태 변경."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = %s WHERE id = %s",
                (status, orderId),
            )

    def getOrder(self, orderId):
        """주문 하나 상세 조회 (품목 포함). 없으면 None."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(_ORDER_SELECT + "WHERE o.id = %s", (orderId,))
            order = cur.fetchone()
            if order is None:
                return None
            order["items"] = self._getOrderItems(cur, orderId)
            return order

    def getOrdersByCard(self, cardId):
        """카드별 주문 이력(카드 태그로 본인 주문 확인)."""
        return self._listOrders("WHERE o.card_id = %s", (cardId,))

    def getOrdersByStatus(self, status):
        """상태별 조회. 서버 재시작 시 진행 중 주문 복구용으로 사용."""
        return self._listOrders("WHERE o.status = %s", (status,))

    def getAllOrders(self):
        """전체 주문 (최신순)."""
        return self._listOrders("", ())

    def _listOrders(self, whereClause, params):
        """getOrdersByCard/ByStatus/getAllOrders가 공통으로 쓰는 내부 함수."""
        conn = self._connect()
        with conn.cursor() as cur:
            sql = _ORDER_SELECT + f"{whereClause} ORDER BY o.id DESC"
            cur.execute(sql, params)
            orders = cur.fetchall()
            for order in orders:
                order["items"] = self._getOrderItems(cur, order["id"])
            return orders

    def _getOrderItems(self, cur, orderId):
        """order_item + product 조인해서 품목 상세(이름, 가격 포함) 가져오기."""
        cur.execute(
            "SELECT oi.product_id AS productId, oi.qty, p.name, p.price "
            "FROM order_item oi JOIN product p ON oi.product_id = p.id "
            "WHERE oi.order_id = %s",
            (orderId,),
        )
        return cur.fetchall()

    # ------------------------------------------------------------
    # 픽업 슬롯
    # ------------------------------------------------------------

    def findFreeSlot(self):
        """빈 슬롯 번호(1~3) 찾기. 다 차면 None."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT assigned_slot FROM orders WHERE assigned_slot IS NOT NULL"
            )
            occupied = {row["assigned_slot"] for row in cur.fetchall()}
            for slot in (1, 2, 3):
                if slot not in occupied:
                    return slot
            return None

    def assignSlot(self, orderId, slot):
        """주문에 픽업 슬롯 배정."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET assigned_slot = %s WHERE id = %s",
                (slot, orderId),
            )

    def releaseSlot(self, slot):
        """픽업 완료 시 슬롯 해제."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET assigned_slot = NULL WHERE assigned_slot = %s",
                (slot,),
            )

    # ------------------------------------------------------------
    # 테스트용
    # ------------------------------------------------------------

    def resetTestData(self, defaultStock=20):
        """개발/테스트 중 쌓인 주문을 지우고 재고를 defaultStock 으로 되돌린다.
        product 는 안 건드림."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("TRUNCATE TABLE order_item")
            cur.execute("TRUNCATE TABLE orders")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            cur.execute("UPDATE product SET stock = %s", (defaultStock,))

    def close(self):
        """서버 종료 시 정리용. 평소엔 연결을 계속 들고 있는 게 정상이다."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


if __name__ == "__main__":
    db = DBManager()
    print("상품 목록:", db.getProducts())
    print("전체 주문:", db.getAllOrders())
