-- hackathontrip.pois
-- Independent database for the hackathon trip planner (D-006 / D-014).
-- This file is CREATE TABLE only. Do not put passwords here.
-- Charset must be utf8mb4 for CJK names.

CREATE TABLE IF NOT EXISTS pois (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Row id / primary key',
  slug VARCHAR(80) NULL COMMENT 'Stable business id, e.g. senso-ji; NULL for search-written rows',
  name VARCHAR(200) NOT NULL COMMENT 'Display name; demo data is mostly English',
  name_local VARCHAR(200) NULL COMMENT 'Name in the local language, used to match CJK searches',
  city VARCHAR(32) NOT NULL COMMENT 'tokyo/kyoto/osaka/seoul/busan/shanghai/beijing/hongkong',
  area VARCHAR(80) NULL COMMENT 'District, e.g. Asakusa / Shibuya',
  lat DECIMAL(10, 7) NOT NULL COMMENT 'Latitude',
  lng DECIMAL(10, 7) NOT NULL COMMENT 'Longitude',
  requires_ticket ENUM('yes', 'no', 'unknown') NOT NULL DEFAULT 'unknown' COMMENT 'Whether admission is required',
  ticket_price DECIMAL(10, 2) NULL COMMENT 'Admission price; NULL when unknown — never invent one',
  ticket_currency CHAR(3) NULL COMMENT 'JPY/KRW/CNY/HKD, only set when a price exists',
  opening_hours VARCHAR(512) NULL COMMENT 'Opening hours, as scraped',
  rating DECIMAL(3, 2) NULL COMMENT 'Rating, optional',
  suggested_duration_min SMALLINT UNSIGNED NULL COMMENT 'Suggested visit duration in minutes',
  category VARCHAR(40) NULL COMMENT 'temple/park/market and the like',
  source ENUM('seed', 'search') NOT NULL DEFAULT 'seed' COMMENT 'seed = preloaded catalog, search = written by a user search',
  place_id VARCHAR(128) NULL COMMENT 'External place id, used for dedupe',
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
  id INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Row id / primary key',
  slug VARCHAR(80) NULL COMMENT 'Stable business id',
  name VARCHAR(200) NOT NULL COMMENT 'Display name; demo data is mostly English',
  name_local VARCHAR(200) NULL COMMENT 'Name in the local language',
  city VARCHAR(32) NOT NULL COMMENT 'tokyo/kyoto/osaka/seoul/busan/shanghai/beijing/hongkong',
  area VARCHAR(80) NULL COMMENT 'District',
  lat DECIMAL(10, 7) NOT NULL COMMENT 'Latitude',
  lng DECIMAL(10, 7) NOT NULL COMMENT 'Longitude',
  kind ENUM('hostel', 'capsule', 'guesthouse', 'hotel', 'unknown') NOT NULL DEFAULT 'unknown' COMMENT 'Lodging type',
  listed TINYINT(1) NOT NULL DEFAULT 1 COMMENT '1 = catalog hostel/capsule shown by default; 0 = hotel written by a user search',
  price_per_night DECIMAL(10, 2) NULL COMMENT 'Reference price per night; NULL when unknown — never invent one',
  price_currency CHAR(3) NULL COMMENT 'JPY/KRW/CNY/HKD, only set when a price exists',
  rating DECIMAL(3, 2) NULL COMMENT 'Rating, optional',
  source ENUM('seed', 'search') NOT NULL DEFAULT 'seed' COMMENT 'seed = preloaded catalog, search = written by a user search',
  place_id VARCHAR(128) NULL COMMENT 'External place id, used for dedupe',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_city_slug (city, slug),
  UNIQUE KEY uk_place_id (place_id),
  KEY idx_city_listed (city, listed),
  KEY idx_city_name (city, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
