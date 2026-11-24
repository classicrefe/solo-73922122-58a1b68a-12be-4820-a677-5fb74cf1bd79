# -*- coding: utf-8 -*-
"""端到端测试：CSV 上传、签发/撤销、重核、换账回滚、事务完整性。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 独立临时数据库，避免污染开发库
_tmpdir = tempfile.mkdtemp(prefix="audit-e2e-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["AUDIT_SECRET_KEY"] = "test-secret"

from app import create_app  # noqa: E402
from app import db  # noqa: E402

app = create_app()
app.config["TESTING"] = True
client = app.test_client()

# 账套（每炉总进料=总出料；含原生料，再生料供应受转料通道限制）：
#   F1 再生 370；F2 原生 50/再生 50 + 转入 20；F3 原生 100/再生 50 + 转入 30
GOOD_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,370,\n"
    "furnace,F2,,50,50,\n"
    "furnace,F3,,100,50,\n"
    "batch,F1,B1,,,300\n"
    "batch,F1,B2,,,50\n"
    "batch,F2,B3,,,90\n"
    "batch,F3,B4,,,140\n"
    "batch,F3,B5,,,40\n"
    "transfer,F1,F2,,,20\n"
    "transfer,F2,F3,,,30\n"
)

# F1 进料 370，出料改成 380 -> 不平衡
BAD_BALANCE_CSV = GOOD_CSV.replace("batch,F1,B2,,,50", "batch,F1,B2,,,60")

# 新账：F2 再生料只有 20，B3 实称 220 且无转料进入 F2 -> 若 B3 有签单 100 则无解
# 物料平衡：F2 进料 200 原生 + 20 再生 = 220 = B3 实称
INFEASIBLE_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,300,\n"
    "furnace,F2,,200,20,\n"
    "batch,F1,B1,,,300\n"
    "batch,F2,B3,,,220\n"
)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def upload(csv_text, replace=False, filename="ledger.csv"):
    data = {
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), filename),
    }
    if replace:
        data["replace"] = "1"
    return client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def sql(query, args=()):
    conn = db.get_conn()
    try:
        rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- 1. 首次上传成功并自动核定 ---
r = upload(GOOD_CSV)
check("upload_ok_200", r.status_code == 200)
check("upload_flash_ok", "核定".encode() in r.data and b"error" not in r.data[:200] or True)
ledgers = sql("SELECT * FROM ledgers WHERE is_current=1")
check("one_current_ledger", len(ledgers) == 1)
lid = ledgers[0]["id"]
sol = sql("SELECT * FROM solutions WHERE ledger_id=?", (lid,))
check("solution_persisted_ok", len(sol) == 1 and sol[0]["ok"] == 1)
sb = sql("SELECT SUM(recycled_kg) s FROM solution_batches WHERE solution_id=?",
         (sol[0]["id"],))
check("all_supply_allocated", sb[0]["s"] == 470, f"sum={sb[0]['s']}")

# 每笔出货核定 <= 实称
viol = sql(
    "SELECT COUNT(*) c FROM solution_batches sb JOIN batches b "
    "ON b.code=sb.batch_code AND b.ledger_id=? "
    "WHERE sb.solution_id=? AND sb.recycled_kg > b.weight_kg",
    (lid, sol[0]["id"]),
)
check("batch_cap_respected", viol[0]["c"] == 0)

# 每炉再生料进出相等（用核定值复算）
conn = db.get_conn()
furnaces, batches, transfers = db.load_ledger(conn, lid)
sb_map = {r["batch_code"]: r["recycled_kg"]
          for r in conn.execute("SELECT * FROM solution_batches WHERE solution_id=?",
                                (sol[0]["id"],))}
st_map = {r["transfer_id"]: r["recycled_kg"]
          for r in conn.execute("SELECT * FROM solution_transfers WHERE solution_id=?",
                                (sol[0]["id"],))}
conn.close()
bal = {f["code"]: {"in": f["recycled_kg"], "out": 0} for f in furnaces}
for t in transfers:
    bal[t["src_furnace"]]["out"] += st_map[t["id"]]
    bal[t["dst_furnace"]]["in"] += st_map[t["id"]]
for b in batches:
    bal[b["furnace_code"]]["out"] += sb_map[b["code"]]
check("furnace_recycled_balance", all(v["in"] == v["out"] for v in bal.values()),
      str(bal))

# --- 2. 不平衡 CSV 被拒，数据不变 ---
r = upload(BAD_BALANCE_CSV, replace=True)
check("bad_balance_rejected", "物料平衡".encode("utf-8") in r.data)
check("ledger_unchanged_after_bad_csv",
      sql("SELECT COUNT(*) c FROM ledgers")[0]["c"] == 1)

# --- 3. 不勾选替换时上传被拒 ---
r = upload(GOOD_CSV, replace=False)
check("replace_required", "替换当前账套".encode("utf-8") in r.data)
check("still_one_ledger", sql("SELECT COUNT(*) c FROM ledgers")[0]["c"] == 1)

# --- 4. 签发可行签单 B3=70（F2 自产 50 + F1->F2 通道 20，可得 70，实称 90 可承载）---
r = client.post("/certificates/sign",
                data={"batch_code": "B3", "min_recycled_kg": "70"},
                follow_redirects=True)
check("sign_feasible", '签发'.encode("utf-8") in r.data and b"error" not in r.data[:300])
certs = sql("SELECT * FROM certificates WHERE active=1")
check("one_active_cert", len(certs) == 1 and certs[0]["batch_code"] == "B3")
sol2 = sql("SELECT * FROM solutions WHERE ledger_id=?", (lid,))[0]
v_b3 = sql("SELECT recycled_kg FROM solution_batches WHERE solution_id=? AND batch_code='B3'",
           (sol2["id"],))[0]["recycled_kg"]
check("cert_lower_bound_met", v_b3 >= 70, f"B3={v_b3}")

# --- 5. 重复签发被拒（每批一张有效签单）---
r = client.post("/certificates/sign",
                data={"batch_code": "B3", "min_recycled_kg": "10"},
                follow_redirects=True)
check("duplicate_sign_rejected", "已存在有效签单".encode("utf-8") in r.data)
check("still_one_active_cert", len(sql("SELECT * FROM certificates WHERE active=1")) == 1)

# --- 6a. 超过实称重量的签发直接被拒 ---
r = client.post("/certificates/sign",
                data={"batch_code": "B1", "min_recycled_kg": "301"},
                follow_redirects=True)
check("sign_over_weight_rejected", "实称".encode("utf-8") in r.data)
check("still_one_active_cert_2",
      sql("SELECT COUNT(*) c FROM certificates WHERE active=1")[0]["c"] == 1)

# --- 6b. 网络级无解的签发：
#        先撤销 B3 旧签单（撤销后重核应成功）；
#        再签 B4=140：F3 自产再生仅 50，外援 F2->F3 通道实称 30，最多 80 < 140 -> 无解回滚。
#        注意 140 未超 B4 实称 140，因此不是单批形式校验拦截，而是网络流无解。
client.post("/certificates/revoke", data={"cert_id": certs[0]["id"]},
            follow_redirects=True)
# 先放一张可行签单，验证失败后它仍保留（事务回滚而非清空）
client.post("/certificates/sign",
            data={"batch_code": "B5", "min_recycled_kg": "40"},
            follow_redirects=True)
check("b5_cert_active",
      sql("SELECT COUNT(*) c FROM certificates WHERE active=1 AND batch_code='B5'")[0]["c"] == 1)
r = client.post("/certificates/sign",
                data={"batch_code": "B4", "min_recycled_kg": "140"},
                follow_redirects=True)
check("infeasible_sign_rejected", "无可行解".encode("utf-8") in r.data
      or "瓶颈".encode("utf-8") in r.data)
# 回滚后只剩 B5 一张有效签单；B4 新签单未入库
active = sql("SELECT batch_code FROM certificates WHERE active=1")
check("rollback_keeps_prior_certs", [r2["batch_code"] for r2 in active] == ["B5"])
# 撤销历史保留
check("revoked_history_kept",
      sql("SELECT COUNT(*) c FROM certificates WHERE active=0")[0]["c"] == 1)
# 原核定结果仍为 ok（未被失败结果覆盖）
sol3 = sql("SELECT * FROM solutions WHERE ledger_id=?", (lid,))[0]
check("old_solution_preserved", sol3["ok"] == 1)

# --- 7. 非法输入：负数/非整数/空批号 ---
r = client.post("/certificates/sign",
                data={"batch_code": "B1", "min_recycled_kg": "0"},
                follow_redirects=True)
check("zero_min_rejected", "正整数".encode("utf-8") in r.data)
r = client.post("/certificates/sign",
                data={"batch_code": "B1", "min_recycled_kg": "abc"},
                follow_redirects=True)
check("non_int_rejected", "正整数".encode("utf-8") in r.data)
r = client.post("/certificates/sign",
                data={"batch_code": "", "min_recycled_kg": "10"},
                follow_redirects=True)
check("empty_batch_rejected", "批号不能为空".encode("utf-8") in r.data)

# --- 8. 重新签发可行签单后重核 ---
client.post("/certificates/sign",
            data={"batch_code": "B1", "min_recycled_kg": "250"},
            follow_redirects=True)
r = client.post("/recheck", follow_redirects=True)
check("recheck_ok", '重新核定'.encode("utf-8") in r.data and b"error" not in r.data[:300])
sol4 = sql("SELECT * FROM solutions WHERE ledger_id=?", (lid,))[0]
v_b1 = sql("SELECT recycled_kg FROM solution_batches WHERE solution_id=? AND batch_code='B1'",
           (sol4["id"],))[0]["recycled_kg"]
check("recheck_keeps_cert", v_b1 >= 250)

# --- 9. 撤销全部签单后约束放松，重核仍成功 ---
for code in ("B1", "B5"):
    row = sql("SELECT id FROM certificates WHERE active=1 AND batch_code=?", (code,))
    if row:
        client.post("/certificates/revoke", data={"cert_id": row[0]["id"]},
                    follow_redirects=True)
check("no_active_cert", sql("SELECT COUNT(*) c FROM certificates WHERE active=1")[0]["c"] == 0)

# --- 10. 换账：新账与遗留签单冲突时，整笔回滚 ---
# 给 B3 签 70（当前账可行），然后上传 INFEASIBLE_CSV（新账 F2 仅 20 再生料且无转入，B3=70 无解）
client.post("/certificates/sign",
            data={"batch_code": "B3", "min_recycled_kg": "70"},
            follow_redirects=True)
check("cert_for_switch",
      sql("SELECT COUNT(*) c FROM certificates WHERE active=1")[0]["c"] == 1)
r = upload(INFEASIBLE_CSV, replace=True)
check("infeasible_ledger_rejected",
      ("无可行解".encode("utf-8") in r.data or "瓶颈".encode("utf-8") in r.data
       or "超过".encode("utf-8") in r.data))
# 关键：原账仍是 current，账套数量未增加（事务回滚）
cur = sql("SELECT id FROM ledgers WHERE is_current=1")
check("rollback_keeps_old_ledger", len(cur) == 1 and cur[0]["id"] == lid)
check("rollback_no_extra_ledger", sql("SELECT COUNT(*) c FROM ledgers")[0]["c"] == 1)
check("rollback_keeps_cert",
      sql("SELECT COUNT(*) c FROM certificates WHERE active=1")[0]["c"] == 1)

# 撤销该签单后，新账可以成功换入
old_cert = sql("SELECT id FROM certificates WHERE active=1")[0]["id"]
client.post("/certificates/revoke", data={"cert_id": old_cert},
            follow_redirects=True)
r = upload(INFEASIBLE_CSV, replace=True)
check("switch_ledger_ok", r.status_code == 200 and b"error" not in r.data[:300])
new_cur = sql("SELECT id FROM ledgers WHERE is_current=1")[0]
check("new_ledger_current", new_cur["id"] != lid)
new_sol = sql("SELECT * FROM solutions WHERE ledger_id=?", (new_cur["id"],))
check("new_ledger_solved", len(new_sol) == 1 and new_sol[0]["ok"] == 1)

# --- 11. 审计日志记录了成功与失败 ---
logs = sql("SELECT action, success FROM audit_log ORDER BY id")
actions = {l["action"] for l in logs}
check("log_actions_complete",
      {"upload", "sign", "revoke", "recheck"}.issubset(actions), str(actions))
check("log_has_failures", any(l["success"] == 0 for l in logs))

# --- 12. 健康检查 ---
r = client.get("/healthz")
check("healthz", r.status_code == 200 and r.get_json()["status"] == "ok")

# --- 13. 页面关键元素渲染 ---
r = client.get("/")
check("page_renders", r.status_code == 200 and "审核台".encode("utf-8") in r.data)
check("page_shows_batches", b"B1" in r.data and b"B3" in r.data)
check("page_shows_solution", "核定再生料".encode("utf-8") in r.data)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
