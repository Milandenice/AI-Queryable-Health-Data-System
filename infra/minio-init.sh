#!/bin/sh
set -eu

attempt=0
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "MinIO did not become ready after 30 attempts" >&2
    exit 1
  fi
  sleep 2
done

mc mb --ignore-existing local/health-raw
mc mb --ignore-existing local/health-curated

for policy in generator etl api; do
  mc admin policy create local "$policy" "/infra/policies/$policy.json"
done

create_user() {
  access_key=$1
  secret_key=$2
  policy=$3

  if ! mc admin user info local "$access_key" >/dev/null 2>&1; then
    mc admin user add local "$access_key" "$secret_key"
  fi
  mc admin policy attach local "$policy" --user "$access_key"
}

create_user "$GENERATOR_ACCESS_KEY" "$GENERATOR_SECRET_KEY" generator
create_user "$ETL_ACCESS_KEY" "$ETL_SECRET_KEY" etl
create_user "$API_ACCESS_KEY" "$API_SECRET_KEY" api

echo "MinIO buckets and least-privilege service users are ready"
