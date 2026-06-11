-- grafana_ro para o database nossagrana_prod. ATENCAO: nossagrana_prod NAO tem
-- Postgres dedicado no namespace nossagrana; vive no MESMO Postgres compartilhado
-- (postgres.database.svc.cluster.local) que o nossalista. O app nossagrana o acessa
-- via NodePort (Service postgres-nodeport, ns database). Aplicar este DDL via
-- kubectl exec no pod do ns database, -d nossagrana_prod.
-- Idempotente: re-rodar atualiza a senha. Senha via psql -v pw=...
-- READ-ONLY: somente CONNECT + USAGE + SELECT. Nenhuma permissão de escrita.

-- Cria o role só se ainda não existir (DO block sem interpolação de senha).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    CREATE ROLE grafana_ro LOGIN;
  END IF;
END $$;

-- Define/atualiza a senha fora do bloco dollar-quoted, onde o psql interpola
-- :'pw' corretamente (dentro de $$...$$ a substituição NÃO acontece).
ALTER ROLE grafana_ro WITH LOGIN PASSWORD :'pw';

GRANT CONNECT ON DATABASE nossagrana_prod TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM grafana_ro;
