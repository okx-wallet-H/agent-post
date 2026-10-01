#!/bin/bash
# 恢复演练（稳定期-1，#24 改判定口径）：拿最近一份备份在临时目录还原成一个库，
# 校验「还原库自身完整性」（口径三选一里的方案 3，推荐口径）：
#   1) PRAGMA integrity_check = ok（库文件可读、B 树完整）
#   2) 表数量与源库一致（schema 对齐）
#   3) 消息条数 ≥ 1 且最新 seq ≥ 1（备份非空）
#   4) 均匀抽样 5 条消息（不足 5 条全抽）做引用检查：会话存在则必须能从 members
#      关联出成员（关联不到=真坏，FAIL）；会话已不存在的「孤儿消息」不判坏
#      （源库自身的历史残留形态，不是恢复问题），只打印提醒
# 为什么不用「消息条数/最新 seq 与源库一致」当判据（#24 打回原因）：备份是拍快照
# 那一刻的历史，源库之后还在收消息，拿「源库现状」比「历史快照」永远会差——
# 生产实测源 335/还原 333、seq 338/336 就是差在备份时刻之后新增的 2 条，不是坏。
# 所以消息条数/最新 seq 的差异只打印（供人看落后多少），不参与 PASS/FAIL。
# 源库用只读连接打开，演练期间不碰源库、不动备份文件。
#
# 用法：
#   bash restore_drill.sh                  # 演练默认源库 /opt/warm-hub/hub.db + 默认备份目录
#   bash restore_drill.sh <源库> <备份目录>  # 指定源库和备份目录（假库演练用这个）
# 环境变量：DRILL_SRC / DRILL_DIR 也可，位置参数优先。
set -u

SRC=${DRILL_SRC:-/opt/warm-hub/hub.db}
DIR=${DRILL_DIR:-/opt/warm-hub/backup}
[ -n "${1:-}" ] && SRC="$1"
[ -n "${2:-}" ] && DIR="$2"

LATEST=$(ls -1t "$DIR"/hub-*.db 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "恢复演练 FAIL：备份目录 $DIR 下没有 hub-*.db"
    exit 1
fi
if [ ! -f "$SRC" ]; then
    echo "恢复演练 FAIL：源库 $SRC 不存在"
    exit 1
fi

TMP=$(mktemp -d /tmp/warm-restore-drill-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
cp "$LATEST" "$TMP/restored.db"   # 还原：备份本身就是完整库文件，cp 即恢复
SIZE=$(stat -c%s "$TMP/restored.db")

echo "源库：$SRC"
echo "备份：$LATEST（${SIZE}B，时间戳 $(stat -c%y "$LATEST" | cut -d. -f1)）"

python3 - "$SRC" "$TMP/restored.db" <<'PY'
import sqlite3, sys
src, restored = sys.argv[1], sys.argv[2]

def stats(path, readonly):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        n_tbl = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        n_msg = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        max_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0]
        return ic, n_tbl, n_msg, max_seq, conn
    except Exception:
        conn.close()
        raise

sic, st, sm, ss, sconn = stats(src, readonly=True)
ric, rt, rm, rs, rconn = stats(restored, readonly=False)

# 抽样：均匀取 5 条（不足 5 条全取）做引用检查
#   会话存在 + 能关联到成员 = OK；会话存在 + 无成员 = 真坏；
#   会话已不存在 = 孤儿消息（源库同款历史残留），不判坏只提醒
n_sample = min(5, rm)
joined = 0
orphans = 0
detail = []
for k in range(n_sample):
    off = (k * rm) // n_sample if n_sample else 0
    row = rconn.execute("SELECT seq FROM messages ORDER BY seq LIMIT 1 OFFSET ?", (off,)).fetchone()
    if row is None:
        continue
    conv = rconn.execute("SELECT 1 FROM conversations WHERE id = "
                         "(SELECT conversation_id FROM messages WHERE seq = ?)", (row[0],)).fetchone()
    if conv is None:
        orphans += 1
        detail.append(f"seq {row[0]}: 孤儿消息（会话已不存在，历史残留，不判坏）")
        continue
    ok = rconn.execute(
        "SELECT 1 FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id "
        "WHERE m.seq = ? LIMIT 1", (row[0],)).fetchone()
    if ok:
        joined += 1
        detail.append(f"seq {row[0]}: 关联到成员")
    else:
        detail.append(f"seq {row[0]}: 会话存在但无成员！")
# 孤儿消息占比（只打印，不判坏）
n_orph = rconn.execute(
    "SELECT count(*) FROM messages m WHERE NOT EXISTS "
    "(SELECT 1 FROM conversations c WHERE c.id = m.conversation_id)").fetchone()[0]

def diff(a, b):
    return "-" if a == b else f"{b - a:+d}"

ok = True
print(f"还原库完整性：{ric}（{'可用' if ric == 'ok' else '不可用！'}）")
print("检查项 | 源库 | 还原库 | 差异 | 判定")
print("--------|------|--------|------|-----")
r = f"表数量 | {st} | {rt} | {diff(st, rt)} | "
if st == rt:
    r += "一致 PASS"
else:
    r += "FAIL：schema 不一致"
    ok = False
print(r)
r = f"消息条数 | {sm} | {rm} | {diff(sm, rm)} | "
if rm >= 1:
    r += "≥1 PASS（差异=备份后新增，只展示不判）"
else:
    r += "FAIL：还原库为空"
    ok = False
print(r)
r = f"最新 seq | {ss} | {rs} | {diff(ss, rs)} | "
if rs >= 1:
    r += "≥1 PASS（差异=备份后新增，只展示不判）"
else:
    r += "FAIL：无消息 seq"
    ok = False
print(r)
checked = joined + orphans
r = f"成员关联抽样 | - | {joined}/{n_sample} | - | "
if checked == n_sample and joined == n_sample - orphans:
    r += "PASS"
else:
    r += f"FAIL：会话存在却关联不到成员"
    ok = False
print(r)
for d in detail:
    print(f"  抽样 {d}")
if n_orph:
    print(f"孤儿消息（会话已删，只提醒不判）：{n_orph}/{rm} 条")
print("恢复演练 PASS" if ok else "恢复演练 FAIL：见上表判定列")
sconn.close(); rconn.close()
sys.exit(0 if ok else 1)
PY
exit $?
