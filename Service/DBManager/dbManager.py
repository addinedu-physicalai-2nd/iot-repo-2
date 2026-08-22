"""
db_manager.py

사용 전 준비:
    pip install pymysql python-dotenv dbutils

central_service/.env (커밋 금지, .gitignore에 추가):
    DB_HOST=xxxx.rds.amazonaws.com
    DB_PORT=3306
    DB_USER=admin
    DB_PASSWORD=password
    DB_NAME=smartmart
"""

import os
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

load_dotenv()


class DBManager:
    """SmartMart DB(member/product/order/order_item)에 대한 CRUD 모음.

    mainService 안에서 한 번만 생성해서(self.db = DBManager()) 계속
    재사용한다. 요청마다 새로 만들지 않는다 
    """

    def __init__(self):
        # TCP 스레드 / Serial 스레드 등 여러 스레드에서 동시에 DB를 쓸 수
        # 있으므로 커넥션 1개를 공유하지 않고 커넥션 풀을 사용한다.
        self._pool = PooledDB(
            creator=pymysql,
            maxconnections=10,
            mincached=2,
            blocking=True,
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", 3306)),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )

    @contextmanager
    def _cursor(self):
        """풀에서 커넥션을 빌려 cursor를 내주고, 끝나면 자동 반납한다.
        mainService에서 직접 호출하지 않는다 (내부 전용)."""
        conn = self._pool.connection()
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            conn.close()  # 실제 연결을 끊는 게 아니라 풀에 반납하는 것

    # ------------------------------------------------------------------
    # member
    # ------------------------------------------------------------------
    def createMember(self, username: str, password: str, name: str,
                      contact: str = None, isAdmin: bool = False) -> int:
        """회원 생성 후 새로 만들어진 member.id를 반환"""
        sql = """
            INSERT INTO member (username, password, name, contact, is_admin)
            VALUES (%s, %s, %s, %s, %s)
        """
        with self._cursor() as cur:
            cur.execute(sql, (username, password, name, contact, isAdmin))
            return cur.lastrowid

    def getMemberByUsername(self, username: str) -> dict | None:
        """로그인 시 사용. 없으면 None."""
        sql = "SELECT * FROM member WHERE username = %s"
        with self._cursor() as cur:
            cur.execute(sql, (username,))
            return cur.fetchone()

    def getMemberById(self, memberId: int) -> dict | None:
        sql = "SELECT * FROM member WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (memberId,))
            return cur.fetchone()

    def listMembers(self) -> list[dict]:
        """관리자 GUI의 get_members 요청에 사용 (mainService 경유)."""
        sql = "SELECT * FROM member ORDER BY created_at DESC"
        with self._cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def usernameExists(self, username: str) -> bool:
        """회원가입 시 중복 아이디 체크."""
        sql = "SELECT 1 FROM member WHERE username = %s"
        with self._cursor() as cur:
            cur.execute(sql, (username,))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # product
    # ------------------------------------------------------------------
    def createProduct(self, name: str, price: int, stock: int = 0) -> int:
        sql = "INSERT INTO product (name, price, stock) VALUES (%s, %s, %s)"
        with self._cursor() as cur:
            cur.execute(sql, (name, price, stock))
            return cur.lastrowid

    def getProduct(self, productId: int) -> dict | None:
        sql = "SELECT * FROM product WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (productId,))
            return cur.fetchone()

    def listProducts(self) -> list[dict]:
        """고객 GUI의 get_products, 관리자 GUI 재고 조회에 사용."""
        sql = "SELECT * FROM product ORDER BY id"
        with self._cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def updateStock(self, productId: int, newStock: int) -> None:
        """관리자 GUI의 update_stock 요청에 사용."""
        sql = "UPDATE product SET stock = %s WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (newStock, productId))

    def decreaseStock(self, productId: int, qty: int) -> bool:
        """
        결제 성공 시 재고 차감. 재고가 부족하면 차감하지 않고 False 반환.
        WHERE 절에 stock >= qty 조건을 걸어서, 동시에 여러 주문이 들어와도
        재고가 마이너스로 내려가지 않도록 방어한다.
        """
        sql = """
            UPDATE product SET stock = stock - %s
            WHERE id = %s AND stock >= %s
        """
        with self._cursor() as cur:
            affected = cur.execute(sql, (qty, productId, qty))
            return affected > 0

    # ------------------------------------------------------------------
    # order
    # ------------------------------------------------------------------
    def createOrder(self, memberId: int, totalPrice: int) -> int:
        """
        주문 생성. 최초 상태는 '대기'.
        상품 목록은 order_item에 별도로 addOrderItem()으로 넣는다.
        """
        sql = """
            INSERT INTO `order` (member_id, status, total_price)
            VALUES (%s, '대기', %s)
        """
        with self._cursor() as cur:
            cur.execute(sql, (memberId, totalPrice))
            return cur.lastrowid

    def getOrder(self, orderId: int) -> dict | None:
        sql = "SELECT * FROM `order` WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (orderId,))
            return cur.fetchone()

    def listOrdersByMember(self, memberId: int) -> list[dict]:
        """고객 GUI의 get_history 요청에 사용."""
        sql = """
            SELECT * FROM `order`
            WHERE member_id = %s
            ORDER BY created_at DESC
        """
        with self._cursor() as cur:
            cur.execute(sql, (memberId,))
            return cur.fetchall()

    def listAllOrders(self) -> list[dict]:
        """관리자 GUI의 get_all_orders 요청에 사용."""
        sql = "SELECT * FROM `order` ORDER BY created_at DESC"
        with self._cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def listActiveOrders(self) -> list[dict]:
        """
        '완료'가 아닌 주문만 조회.
        서버 재시작 시 mainService.restoreFromDb()가 메모리(orders dict)를
        복구할 때 이걸로 진행 중인 주문만 불러오면 된다.
        """
        sql = "SELECT * FROM `order` WHERE status != '완료' ORDER BY created_at"
        with self._cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def updateOrderStatus(self, orderId: int, status: str) -> None:
        """
        status: '대기' | '결제완료' | '출고중' | '픽업대기' | '완료'
        """
        sql = "UPDATE `order` SET status = %s WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (status, orderId))

    def assignSlot(self, orderId: int, slot: int) -> None:
        """픽업 슬롯 배정. mainService.assignPickupSlot()에서 호출."""
        sql = "UPDATE `order` SET assigned_slot = %s WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (slot, orderId))

    def releaseSlot(self, orderId: int) -> None:
        """픽업 완료 후 슬롯 반납. mainService.completePickup()에서 호출."""
        sql = "UPDATE `order` SET assigned_slot = NULL WHERE id = %s"
        with self._cursor() as cur:
            cur.execute(sql, (orderId,))

    # ------------------------------------------------------------------
    # order_item
    # ------------------------------------------------------------------
    def addOrderItem(self, orderId: int, productId: int, qty: int) -> int:
        sql = """
            INSERT INTO order_item (order_id, product_id, qty)
            VALUES (%s, %s, %s)
        """
        with self._cursor() as cur:
            cur.execute(sql, (orderId, productId, qty))
            return cur.lastrowid

    def listOrderItems(self, orderId: int) -> list[dict]:
        """
        order_item과 product를 조인해서 상품명/가격까지 같이 반환.
        Qt에 order_created, history_data 등을 보낼 때 바로 쓰기 좋게.
        """
        sql = """
            SELECT oi.id, oi.order_id, oi.product_id, oi.qty,
                   p.name AS product_name, p.price AS product_price
            FROM order_item oi
            JOIN product p ON p.id = oi.product_id
            WHERE oi.order_id = %s
        """
        with self._cursor() as cur:
            cur.execute(sql, (orderId,))
            return cur.fetchall()


# ----------------------------------------------------------------------
# 간단한 동작 확인용 (python db_manager.py로 직접 실행했을 때만 동작)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    db = DBManager()
    print("상품 목록:", db.listProducts())
    print("진행 중 주문:", db.listActiveOrders())