#!/bin/bash
# 异地备份（稳定期-1）：把最新一份本地备份 rsync 到香港枢纽，远端只保留 7 天。
# 失败写日志 /opt/warm-hub/offsite_backup.log（含 FAIL 行），并发枢纽告警——告警通道
# 与 healthcheck.sh 完全同一套（POST https://warm.hvip.one/events，agent=温暖），
# H 在枢纽一处就能看到「异地备份失败」。token 从 ~/.claude/warm-report.env 读，
# 绝不打印。
#
# 用法：
#   bash offsite_backup.sh            # 真推（rsync 到枢纽 + 远端清理 7 天前文件）
#   bash offsite_backup.sh --dry-run  # 只演示：rsync -n -v 打印传输清单，不真推不真删不发告警
# 环境变量可覆盖：WARM_OFFSITE_DIR（备份目录）、WARM_OFFSITE_HOST（默认 root@38.22.90.145）、
#   WARM_OFFSITE_LOG（日志路径）、WARM_OFFSITE_REMOTE（远端目录，默认 /data/warm-hub-backup/）
set -u

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

DIR=${WARM_OFFSITE_DIR:-/opt/warm-hub/backup}
HOST=${WARM_OFFSITE_HOST:-root@38.22.90.145}
REMOTE=${WARM_OFFSITE_REMOTE:-/data/warm-hub-backup/}
LOG=${WARM_OFFSITE_LOG:-/opt/warm-hub/offsite_backup.log}
HUB=${WARM_HUB_HC_HUB:-https://warm.hvip.one/events}

log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null || true; }

LATEST=$(ls -1t "$DIR"/hub-*.db 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    log "FAIL 本地没有备份（$DIR 下无 hub-*.db）"
    echo "异地备份 FAIL：本地没有备份"
    exit 1
fi

# 远端是 ssh 主机（含 @）还是本地路径（本地路径可用于演练模拟）
case "$HOST" in
    *@*) REMOTE_SH=( ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" ); DST="$HOST:$REMOTE" ;;
    *)   REMOTE_SH=( sh -c ); DST="$REMOTE" ;;
esac

# 推最新一份（rsync-path 保证远端目录存在；dry-run 时 mkdir 不执行写操作只建目录）
RSYNC_OPTS=( -az --timeout=60 --rsync-path="mkdir -p $REMOTE && rsync" )
[ "$DRY" = 1 ] && RSYNC_OPTS+=( -n -v )

OUT=$(rsync "${RSYNC_OPTS[@]}" "$LATEST" "$DST" 2>&1)
RC=$?

if [ "$RC" -ne 0 ]; then
    log "FAIL rsync 退出码 $RC：$LATEST -> $DST"
    log "FAIL 输出摘要：$(echo "$OUT" | tail -2 | tr '\n' ' ')"
    echo "$OUT" | tail -5
    echo "异地备份 FAIL：rsync 失败（退出码 $RC），日志见 $LOG"
    if [ "$DRY" = 1 ]; then
        log "DRY-RUN：失败路径演练，不发告警（dry-run 不打扰枢纽）"
        exit 1
    fi
    # 告警：与 healthcheck.sh 同一通道；token 绝不打印
    token=""
    for f in /home/warm/.claude/warm-report.env /etc/warm-cloud.env; do
        if [ -z "$token" ] && [ -r "$f" ]; then
            token=$(grep -E '^WARM_HUB_TOKEN=' "$f" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
            [ -z "$token" ] && token=$(grep -E '^WARM_CLOUD_TOKEN=' "$f" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
        fi
    done
    if [ -n "$token" ]; then
        curl -s -m 8 -X POST "$HUB" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            --data-binary '{"agent":"温暖","kind":"告警","text":"温暖通信台异地备份失败：rsync 到香港枢纽失败，看 offsite_backup.log"}' \
            >> "$LOG" 2>/dev/null
        echo >> "$LOG" 2>/dev/null || true
    else
        log "FAIL 告警未发出：没找到枢纽 token"
    fi
    exit 1
fi

if [ "$DRY" = 1 ]; then
    echo "$OUT" | grep -E 'hub-[0-9]{8}-[0-9]{2}\.db|total size|would have' | head -6
    echo "远端现状（只读）："
    "${REMOTE_SH[@]}" "ls -1t $REMOTE/hub-*.db 2>/dev/null | head -8; echo ---; ls -1 $REMOTE/hub-*.db 2>/dev/null | wc -l" 2>&1 | head -12
    echo "异地备份 DRY-RUN：以上为将执行的传输（未真推）；7 天前旧档清理将执行 find -mtime +7 -delete"
    log "DRY-RUN OK：$LATEST -> $HOST:$REMOTE（未真推）"
    exit 0
fi

# 远端保留 7 天（按 mtime 删 7 天前的备份；失败只记日志，不影响本次推送结果）
CLEAN=$("${REMOTE_SH[@]}" \
    "find $REMOTE -name 'hub-*.db' -mtime +7 -print -delete 2>/dev/null; ls -1 $REMOTE/hub-*.db 2>/dev/null | wc -l" 2>&1)
CR=$?
if [ "$CR" -ne 0 ]; then
    log "WARN 远端清理失败（本次推送已成功）：$(echo "$CLEAN" | tail -1)"
else
    log "OK $LATEST -> $DST；远端现共 $(echo "$CLEAN" | tail -1) 份"
fi
echo "异地备份 OK：$LATEST -> $DST"
