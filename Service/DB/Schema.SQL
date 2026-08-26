-- SmartMart 스키마
-- 사용법: mysql -h <endpoint> -P 3306 -u admin -p < schema.sql
-- 또는 mysql 클라이언트/워크벤치에서 SOURCE schema.sql

CREATE DATABASE IF NOT EXISTS smartmart
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;

USE smartmart;

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
    card_uid        VARCHAR(8) NULL,   -- RFID 카드 UID(hex). 결제 태그 전까진 NULL
    status          ENUM('대기', '결제완료', '출고중', '픽업대기', '완료', '오류')
                        NOT NULL DEFAULT '대기',
    assigned_slot   INT NULL,
    total_price     INT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at         DATETIME NULL
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
