-- SmartMart 스키마
-- 사용법: mysql -h <endpoint> -P 3306 -u admin -p < schema.sql
-- 또는 mysql 클라이언트/워크벤치에서 SOURCE schema.sql

CREATE DATABASE IF NOT EXISTS smartmart
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;

USE smartmart;

-- ----------------------------------------------------------------------
-- member / card
-- ----------------------------------------------------------------------
-- 회원가입 화면은 없다 — 관리자가 미리 등록해둔 회원만 카드로 주문 가능.
CREATE TABLE IF NOT EXISTS member (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(32) NOT NULL,
    contact     VARCHAR(32),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS card (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    member_id   INT NOT NULL,          -- 한 회원이 카드 여러 장 가능 → UNIQUE 안 걺
    uid         VARCHAR(20) NOT NULL UNIQUE,  -- 카드 물리 UID(hex). 카드 자체는 유일
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_card_member
        FOREIGN KEY (member_id) REFERENCES member(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------
-- product
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product (
    id      INT AUTO_INCREMENT PRIMARY KEY,
    name    VARCHAR(32) NOT NULL,
    price   INT NOT NULL,
    stock   INT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS orders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    card_id         INT NULL,          -- 등록 안 된 카드는 주문 자체를 안 받으니
                                        -- NULL 은 사실상 안 생기지만, FK 라 NULL 허용
    status          ENUM('대기', '결제완료', '출고중', '픽업대기', '완료', '오류')
                        NOT NULL DEFAULT '대기',
    assigned_slot   INT NULL,
    total_price     INT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at         DATETIME NULL,
    balance_before  INT NULL,          -- 결제 시점 카드 잔액(차감 전)
    balance_after   INT NULL,          -- 결제 후 카드 잔액(차감 후)
    CONSTRAINT fk_order_card
        FOREIGN KEY (card_id) REFERENCES card(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------
-- order_item
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_item (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    order_id    INT NOT NULL,
    product_id  INT NOT NULL,
    qty         INT NOT NULL,
    CONSTRAINT fk_order_item_order
        FOREIGN KEY (order_id) REFERENCES orders(id),
    CONSTRAINT fk_order_item_product
        FOREIGN KEY (product_id) REFERENCES product(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
