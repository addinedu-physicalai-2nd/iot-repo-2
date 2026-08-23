-- SmartMart 스키마
-- 사용법: mysql -h <endpoint> -P 3306 -u admin -p < schema.sql
-- 또는 mysql 클라이언트/워크벤치에서 SOURCE schema.sql

CREATE DATABASE IF NOT EXISTS smartmart
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;

USE smartmart;

-- ----------------------------------------------------------------------
-- member
-- ----------------------------------------------------------------------
DROP TABLE IF EXISTS order_item;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS product;
DROP TABLE IF EXISTS member;

CREATE TABLE member (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    username    VARCHAR(32) NOT NULL,
    password    VARCHAR(32) NOT NULL,
    name        VARCHAR(32) NOT NULL,
    contact     VARCHAR(32),
    is_admin    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_member_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------
-- product
-- ----------------------------------------------------------------------
CREATE TABLE product (
    id      INT AUTO_INCREMENT PRIMARY KEY,
    name    VARCHAR(32) NOT NULL,
    price   INT NOT NULL,
    stock   INT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE orders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    member_id       INT NOT NULL,
    status          ENUM('대기', '결제완료', '출고중', '픽업대기', '완료')
                        NOT NULL DEFAULT '대기',
    assigned_slot   INT NULL,
    total_price     INT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_order_member
        FOREIGN KEY (member_id) REFERENCES member(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ----------------------------------------------------------------------
-- order_item
-- ----------------------------------------------------------------------
CREATE TABLE order_item (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    order_id    INT NOT NULL,
    product_id  INT NOT NULL,
    qty         INT NOT NULL,
    CONSTRAINT fk_order_item_order
        FOREIGN KEY (order_id) REFERENCES orders(id),
    CONSTRAINT fk_order_item_product
        FOREIGN KEY (product_id) REFERENCES product(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
