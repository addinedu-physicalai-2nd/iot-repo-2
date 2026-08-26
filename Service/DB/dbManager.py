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
# mainService.Order.fromDbRow() 가 cardUid/assignedSlot 키를 그대로 기대한다.
_ORDER_SELECT = (
    "SELECT id, card_uid AS cardUid, status, "
    "assigned_slot AS assignedSlot, total_price AS totalPrice, "
    "created_at AS createdAt, paid_at AS paidAt FROM orders "
)


class DBManager:
    """product / orders / order_item 에 대한 CRUD 모음.
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

    def createOrder(self, cardUid, items):
        """
        주문 생성. items 예: [{"productId": 1, "qty": 2}, ...]
        cardUid 는 상품 고르기 전에 이미 태그해서 알고 있는 값 — 주문 생성
        시점부터 카드가 비어있는 행이 안 생기게 처음부터 넣는다.
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
                "INSERT INTO orders (card_uid, status, total_price) "
                "VALUES (%s, '대기', %s)",
                (cardUid, total),
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

    def confirmPayment(self, orderId):
        """결제 확정 표시 (paidAt 기록 + status). 재고는 createOrder에서 이미 처리했음."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET paid_at = NOW(), status = '결제완료' "
                "WHERE id = %s",
                (orderId,),
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
            cur.execute(_ORDER_SELECT + "WHERE id = %s", (orderId,))
            order = cur.fetchone()
            if order is None:
                return None
            order["items"] = self._getOrderItems(cur, orderId)
            return order

    def getOrdersByCard(self, cardUid):
        """카드별 주문 이력(카드 태그로 본인 주문 확인)."""
        return self._listOrders("WHERE card_uid = %s", (cardUid,))

    def getOrdersByStatus(self, status):
        """상태별 조회. 서버 재시작 시 진행 중 주문 복구용으로 사용."""
        return self._listOrders("WHERE status = %s", (status,))

    def getAllOrders(self):
        """전체 주문 (최신순)."""
        return self._listOrders("", ())

    def _listOrders(self, whereClause, params):
        """getOrdersByMember/ByStatus/getAllOrders가 공통으로 쓰는 내부 함수."""
        conn = self._connect()
        with conn.cursor() as cur:
            sql = _ORDER_SELECT + f"{whereClause} ORDER BY id DESC"
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
