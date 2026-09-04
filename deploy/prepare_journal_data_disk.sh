#!/usr/bin/env bash
# Prepare the journal data root on the server data disk and bind it into the
# application-writable /var tree. Run as root before starting journal services.
set -euo pipefail

SOURCE="${JOURNAL_DATA_DISK_SOURCE:-/home/data/marx-search-journal}"
TARGET="${JOURNAL_DATA_MOUNT_TARGET:-/var/www/.marx_search_full/journal}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"

case "${SOURCE}" in
  /home/data/*) ;;
  *) echo "refusing non-data-disk source: ${SOURCE}" >&2; exit 2 ;;
esac
case "${TARGET}" in
  /var/www/.marx_search_full/journal) ;;
  *) echo "refusing unexpected journal mount target: ${TARGET}" >&2; exit 2 ;;
esac

data_mount_target="$(findmnt -rn -o TARGET --target /home/data 2>/dev/null || true)"
if [ "${data_mount_target}" != "/home/data" ]; then
  echo "/home/data is not a dedicated mounted data disk; refusing to create journal storage" >&2
  exit 4
fi

install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${SOURCE}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${TARGET}"
for directory in db articles issues tmp logs backups; do
  install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${SOURCE}/${directory}"
done

mounted_source="$(findmnt -rn -M "${TARGET}" -o SOURCE 2>/dev/null || true)"
if [ -n "${mounted_source}" ]; then
  source_device="$(findmnt -rn -o SOURCE --target "${SOURCE}")"
  case "${mounted_source}" in
    "${SOURCE}"|"${source_device}"|"${source_device}"\[*\]) ;;
    *)
    echo "journal target is already mounted from an unexpected source: ${mounted_source}" >&2
    exit 3
    ;;
  esac
else
  mount --bind "${SOURCE}" "${TARGET}"
fi

fstab_line="${SOURCE} ${TARGET} none bind,nofail,x-systemd.requires-mounts-for=/home/data 0 0"
if ! grep -Fqx "${fstab_line}" /etc/fstab; then
  printf '%s\n' "${fstab_line}" >> /etc/fstab
fi

chown -R "${APP_USER}:${APP_GROUP}" "${SOURCE}"
chmod 0700 "${SOURCE}" "${SOURCE}"/{db,articles,issues,tmp,logs,backups}
findmnt --target "${TARGET}"
echo "journal data disk ready: ${SOURCE} -> ${TARGET}"
