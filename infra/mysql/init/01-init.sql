-- MySQL 8 初始化（docker-entrypoint-initdb.d 仅在数据卷首建时执行）
-- chatwork 应用库由 compose MYSQL_DATABASE 自动建（app/app）；
-- 此处补 Keycloak 专用库与账号（compose keycloak 服务使用）
CREATE DATABASE IF NOT EXISTS keycloak
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'keycloak'@'%' IDENTIFIED BY 'keycloak';
GRANT ALL PRIVILEGES ON keycloak.* TO 'keycloak'@'%';
FLUSH PRIVILEGES;
