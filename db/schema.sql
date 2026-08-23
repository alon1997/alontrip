-- hackathontrip.pois
-- Independent database for the hackathon trip planner (D-006 / D-014).
-- This file is CREATE TABLE only. Do not put passwords here.
-- Charset must be utf8mb4 for CJK names.

CREATE TABLE IF NOT EXISTS pois (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '序号/主键',
  slug VARCHAR(80) NULL COMMENT '稳定业务ID，如 senso-ji；检索写入可空',
  name VARCHAR(200) NOT NULL COMMENT '展示名，Demo 以英文为主',
  name_local VARCHAR(200) NULL COMMENT '当地语言名，供中日韩搜索匹配',
  city VARCHAR(32) NOT NULL COMMENT 'tokyo/kyoto/osaka/seoul/busan/shanghai/beijing/hongkong',
  area VARCHAR(80) NULL COMMENT '区域，如 Asakusa / Shibuya',
  lat DECIMAL(10, 7) NOT NULL COMMENT '纬度',
  lng DECIMAL(10, 7) NOT NULL COMMENT '经度',
  requires_ticket ENUM('yes', 'no', 'unknown') NOT NULL DEFAULT 'unknown' COMMENT '是否需要门票',
  ticket_price DECIMAL(10, 2) NULL COMMENT '门票价格，未知则空，禁止编造',
  ticket_currency CHAR(3) NULL COMMENT 'JPY/KRW/CNY/HKD，有票价才填',
  opening_hours VARCHAR(512) NULL COMMENT '营业时间原文',
  rating DECIMAL(3, 2) NULL COMMENT '评分，可选',
  suggested_duration_min SMALLINT UNSIGNED NULL COMMENT '建议停留分钟',
  category VARCHAR(40) NULL COMMENT 'temple/park/market 等',
  source ENUM('seed', 'search') NOT NULL DEFAULT 'seed' COMMENT '预灌或检索写入',
  place_id VARCHAR(128) NULL COMMENT '外部地点ID，用于去重',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_city_slug (city, slug),
  UNIQUE KEY uk_place_id (place_id),
  KEY idx_city (city),
  KEY idx_city_name (city, name),
  KEY idx_city_name_local (city, name_local)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Catalog hostels (listed=1) plus user-added hotels from search (listed=0).
CREATE TABLE IF NOT EXISTS lodgings (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '序号/主键',
  slug VARCHAR(80) NULL COMMENT '稳定业务ID',
  name VARCHAR(200) NOT NULL COMMENT '展示名，Demo 以英文为主',
  name_local VARCHAR(200) NULL COMMENT '当地语言名',
  city VARCHAR(32) NOT NULL COMMENT 'tokyo/kyoto/osaka/seoul/busan/shanghai/beijing/hongkong',
  area VARCHAR(80) NULL COMMENT '区域',
  lat DECIMAL(10, 7) NOT NULL COMMENT '纬度',
  lng DECIMAL(10, 7) NOT NULL COMMENT '经度',
  kind ENUM('hostel', 'capsule', 'guesthouse', 'hotel', 'unknown') NOT NULL DEFAULT 'unknown' COMMENT '住宿类型',
  listed TINYINT(1) NOT NULL DEFAULT 1 COMMENT '1=目录默认展示的青旅/胶囊；0=用户检索写入的酒店',
  price_per_night DECIMAL(10, 2) NULL COMMENT '一晚参考价，未知则空，禁止编造',
  price_currency CHAR(3) NULL COMMENT 'JPY/KRW/CNY/HKD，有价格才填',
  rating DECIMAL(3, 2) NULL COMMENT '评分，可选',
  source ENUM('seed', 'search') NOT NULL DEFAULT 'seed' COMMENT '预灌或检索写入',
  place_id VARCHAR(128) NULL COMMENT '外部地点ID，用于去重',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_city_slug (city, slug),
  UNIQUE KEY uk_place_id (place_id),
  KEY idx_city_listed (city, listed),
  KEY idx_city_name (city, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
