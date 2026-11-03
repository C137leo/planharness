-- ============================================================
-- PlanHarness 数据库初始化（在 Postgres 超级用户下执行一次）
-- ------------------------------------------------------------
-- 推荐用法（自动生成密码、自动生成 env）：deploy/create-db.sh
--
-- 手动用法：
--     psql -U postgres -v app_password='你的强密码' -f deploy/init-db.sql
--
-- 可用变量（都有默认值，可覆盖）：
--     app_password  必填，没传就报错退出
--     app_user      默认 appuser
--     app_db        默认 planharness
--
-- 做三件事：
--   1) 建一个只能登录的专用角色（不用超级用户跑应用）
--   2) 建库，归该角色所有（它才能自己建表，包括 LangGraph 的 checkpoint 表）
--   3) 收紧 public schema 权限
--
-- 幂等：角色和库已存在时跳过，可重复执行。
-- 注意：本脚本**不建业务表** —— 那些由应用启动时的 create_all() 和 saver.setup() 建。
-- ============================================================

-- 没传密码直接退出（不静默用占位符）
\if :{?app_password}
\else
  \echo 'ERROR: 必须用 -v app_password=... 传入数据库密码'
  \q
\endif

-- 传了占位符也拒绝
SELECT :'app_password' = 'CHANGE_ME_STRONG_PASSWORD' AS is_placeholder \gset
\if :is_placeholder
  \echo 'ERROR: 密码不能是占位符 CHANGE_ME_STRONG_PASSWORD'
  \q
\endif
\if :{?app_user}
\else
  \set app_user appuser
\endif
\if :{?app_db}
\else
  \set app_db planharness
\endif

-- 1) 专用角色（幂等）
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user')\gexec

-- 2) 数据库（幂等；CREATE DATABASE 不能在事务里，用 \gexec）
SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_db')\gexec

-- 3) 切到业务库，收紧 schema 权限
\connect :app_db

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO :"app_user";

-- 校验
\echo '--- 角色与数据库 ---'
SELECT rolname, rolcanlogin, rolsuper
FROM pg_roles WHERE rolname = :'app_user';
SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database WHERE datname = :'app_db';
