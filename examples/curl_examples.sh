#!/usr/bin/env bash
# 冷链追责服务 调用示例（curl）。
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_examples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
C="COLD-DEMO-1"

echo "1) 创建货物：编号 / 起始时间 / 温度上限 / 起始保管方"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo

echo "2) 测温（10:00，8.2℃ > 上限 4℃，开启越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "3) 交接（12:00，北京冷库 -> 上海冷库），区间在此刻拆分"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}"; echo

echo "4) 交接（14:00，上海冷库 -> 广州冷库）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T14:00:00+08:00\",
  \"giver\": \"上海冷库\", \"receiver\": \"广州冷库\"
}"; echo

echo "5) 测温（16:00，3.1℃ <= 上限，闭合越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T16:00:00+08:00\", \"temperature\": \"3.1\"
}"; echo

echo "6) 幂等重传第 2) 步（相同内容返回 200 replayed，不重复写入）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "7) 追责报告：越温起止 + 各责任方片段（按时间）"
curl -sS "$BASE/cargo/$C/report"; echo

echo "8) 责任确认查询：已闭合区间（区间标识=开启测温事件ID）、结束事件ID、责任片段、当前快照标识、最新表态、汇总状态"
curl -sS "$BASE/cargo/$C/responsibilities"; echo

# 取首个已闭合区间的区间标识与当前快照标识（需系统带 python3；也可手工从上一步复制）
read -r IID SNAP < <(python3 - "$BASE" "$C" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "区间标识=$IID 快照标识=$SNAP"

echo "9) 北京冷库 确认担责"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "10) 上海冷库 提出异议（异议必须带理由 reason）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"dispute\",
    \"reason\": \"该时段冷机由北京冷库远程设定，上海冷库未接管温控\",
    \"stance_id\": \"stance-sh-1\"
  }"; echo

echo "11) 再次查询：汇总状态变为 has_dispute，各责任方返回最新一条有效表态"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "12) 表态历史：改口用新编号，历次表态（含已改口/已失效）均可查"
curl -sS "$BASE/cargo/$C/responsibilities/$IID/stances"; echo

echo "13) 幂等：同编号同内容重传返回原记录（200 replayed，即使快照随后失效也如此）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "14) 改口：上海冷库改为确认（必须使用新表态编号，携带新编号提交）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-sh-2\"
  }"; echo

echo "15) 最终查询：北京/上海均已确认；广州冷库尚未表态 -> 汇总仍为 pending（待确认）"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "16) 广州冷库也确认 -> 汇总 all_confirmed"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"广州冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-gz-1\"
  }" >/dev/null
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo
echo "================ 本轮新增：车次 / 装卸时段 / 车载测温 ================"
# 演示货物：两票同车货物，温度上限不同（C-A 4℃，C-B 8℃）
CA="COLD-CAR-A"; CB="COLD-CAR-B"; TRIP="TRUCK-DEMO-1"
for CC in "$CA" "$CB"; do
  LIM="4.0"; [ "$CC" = "$CB" ] && LIM="8.0"
  curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
    \"cargo_id\": \"$CC\",
    \"started_at\": \"2026-09-30T08:00:00+08:00\",
    \"temp_upper_limit\": \"$LIM\",
    \"initial_holder\": \"北京冷库\"
  }" >/dev/null
done

echo "17) 登记车次（车牌号）"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$TRIP\",
  \"vehicle_no\": \"京B-冷888\",
  \"note\": \"9月30日 京沪冷链班\"
}"; echo

echo "18) 批量登记两票货物的装卸时段（左闭右开，同一货物不得跨车次重叠）"
curl -sS -X POST "$BASE/trips/$TRIP/manifest" \
  -H 'Content-Type: application/json' -d "{
    \"items\": [
      {\"cargo_id\": \"$CA\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"},
      {\"cargo_id\": \"$CB\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }"; echo

echo "19) 非法时段（起点不早于终点）整批拒绝：422 invalid_loading_window，不留部分记录"
curl -sS -X POST "$BASE/trips/$TRIP/manifest" \
  -H 'Content-Type: application/json' -d "{
    \"items\": [
      {\"cargo_id\": \"$CA\",
       \"loaded_at\": \"2026-09-30T19:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T19:00:00+08:00\"}
    ]
  }"; echo

echo "20) 车载读数 7℃（允许晚到）：回执 affected_cargo_ids 给出读数时刻仍在车上的货物"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-1\",
  \"trip_id\": \"$TRIP\",
  \"read_at\": \"2026-09-30T10:00:00+08:00\",
  \"temperature\": \"7.0\"
}"; echo

echo "21) 车载读数幂等重传（同编号同内容 -> 200 replayed）"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-1\",
  \"trip_id\": \"$TRIP\",
  \"read_at\": \"2026-09-30T10:00:00+08:00\",
  \"temperature\": \"7.0\"
}"; echo

echo "22) 同刻自有测温优先：$CA 在 11:00 有自有测温 3.1℃，车载 7℃ 读数对其被取代但原始可查"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"ca-own-1\", \"cargo_id\": \"$CA\",
  \"occurred_at\": \"2026-09-30T11:00:00+08:00\", \"temperature\": \"3.1\"
}" >/dev/null
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-2\",
  \"trip_id\": \"$TRIP\",
  \"read_at\": \"2026-09-30T11:00:00+08:00\",
  \"temperature\": \"7.0\"
}" >/dev/null

echo "23) 交接仍只由货物自有事件改变保管方（12:00 北京冷库 -> 上海冷库，两票货物各写一笔）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"ca-h1\", \"cargo_id\": \"$CA\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}" >/dev/null
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"cb-h1\", \"cargo_id\": \"$CB\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}" >/dev/null

echo "24) 车载 3℃ 读数闭合两票货物的越温区间"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-3\",
  \"trip_id\": \"$TRIP\",
  \"read_at\": \"2026-09-30T14:00:00+08:00\",
  \"temperature\": \"3.0\"
}"; echo

echo "25) 车次详情：车载原始读数 + 每笔读数对各货物的适用性 / 是否采用 / 是否越温"
curl -sS "$BASE/trips/$TRIP" | python3 -m json.tool; echo

echo "26) $CA 合并读数时间线（vr-2 同刻被自有测温取代：adopted=false，原始仍列出）"
curl -sS "$BASE/cargo/$CA/timeline" | python3 -m json.tool; echo

echo "27) $CA 追责报告：10:00 车载开启越温（区间标识 veh:vr-1），14:00 车载闭合，跨交接拆分"
curl -sS "$BASE/cargo/$CA/report"; echo
echo "    $CB 上限 8℃，7℃ 全程不越温："
curl -sS "$BASE/cargo/$CB/report"; echo

echo "28) 对车载读数开启的区间（veh:vr-1）查询并提交责任表态"
VSNAP=$(curl -sS "$BASE/cargo/$CA/responsibilities" | python3 -c 'import json,sys; print(json.load(sys.stdin)["intervals"][0]["snapshot_id"])')
curl -sS -X POST "$BASE/cargo/$CA/responsibilities/veh:vr-1/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$VSNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-ca-bj-1\"
  }"; echo
curl -sS "$BASE/cargo/$CA/responsibilities/veh:vr-1/stances"; echo

echo
echo "================ 本轮新增：质控同时越温查询 ================"
# 再挂一票上限 4℃ 的货物 Q 到同一车次（CA 上限 4℃、CB 上限 8℃）
CQ="COLD-CAR-Q"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$CQ\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}" >/dev/null
curl -sS -X POST "$BASE/trips/$TRIP/manifest" -H 'Content-Type: application/json' -d "{
  \"items\": [
    {\"cargo_id\": \"$CQ\",
     \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
     \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
  ]
}" >/dev/null

# 回顾已有读数：10:00 车载 7℃（CA、Q 越温；CB 上限 8℃ 不越）；
# 11:00 CA 有自有 3.1℃ 测温 -> CA 此时刻闭合（同刻车载 7℃ 对 CA 被取代），Q 仍越温；
# 14:00 车载 3℃ -> Q 闭合。
echo "29) 同时越温时段（min_cargo 默认 2）：仅 10:00-11:00 [CA,Q] 两票同时越温"
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00" | python3 -m json.tool; echo

echo "30) 查询窗裁剪（09:30-10:30）：片段被裁到查询窗内"
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T09:30:00+08:00" \
  --data-urlencode "end_at=2026-09-30T10:30:00+08:00" | python3 -m json.tool; echo

echo "31) 门槛 3（min_cargo=3）：CB 上限 8℃ 全程不越温，无三票同时段 -> 空结果"
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00" \
  --data-urlencode "min_cargo=3"; echo

echo "32) 错误示例：未知车次 / 终点不晚于起点 / 门槛非法 / 缺时区"
curl -sS -G "$BASE/trips/NO-TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00"; echo
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-10-01T00:00:00+08:00" \
  --data-urlencode "end_at=2026-09-30T00:00:00+08:00"; echo
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00" \
  --data-urlencode "min_cargo=1"; echo
curl -sS -G "$BASE/trips/$TRIP/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00"; echo

echo
echo "================ 本轮新增：测温值更正（只改温度） ================"

echo "33) 更正自有测温 evt-temp-1：原录 8.2℃ 录错，expected_version=0（首次）改为 3.8℃"
curl -sS -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d "{
  \"correction_id\": \"corr-ca-1\",
  \"target_type\": \"own_event\",
  \"target_id\": \"evt-temp-1\",
  \"expected_version\": 0,
  \"new_temperature\": \"3.8\"
}"; echo

echo "34) 原始记录查询：temperature 仍是原值 8.2，另给 effective_temperature/version"
curl -sS "$BASE/cargo/$C/events" | python3 -m json.tool; echo

echo "35) 原始记录及历次更正可查（按版本升序）"
curl -sS "$BASE/events/evt-temp-1/corrections" | python3 -m json.tool; echo

echo "36) 追责报告按有效值重建：8.2℃ 开启的越温区间消失（3.8 <= 上限 4）"
curl -sS "$BASE/cargo/$C/report"; echo

echo "37) 同更正ID同内容重传 -> 200 replayed（返回原结果）；异内容 -> 409"
curl -sS -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d "{
  \"correction_id\": \"corr-ca-1\",
  \"target_type\": \"own_event\",
  \"target_id\": \"evt-temp-1\",
  \"expected_version\": 0,
  \"new_temperature\": \"3.8\"
}"; echo

echo "38) 期望版本过期（当前为 1，仍传 0）-> 409 stale_version，不落部分结果"
curl -sS -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d "{
  \"correction_id\": \"corr-ca-2\",
  \"target_type\": \"own_event\",
  \"target_id\": \"evt-temp-1\",
  \"expected_version\": 0,
  \"new_temperature\": \"3.5\"
}"; echo

echo "39) 交接不可更正 -> 422 handover_not_correctable"
curl -sS -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d "{
  \"correction_id\": \"corr-h-1\",
  \"target_type\": \"own_event\",
  \"target_id\": \"evt-hand-1\",
  \"expected_version\": 0,
  \"new_temperature\": \"3.5\"
}"; echo

echo "40) 原事件按原内容重传仍幂等（temperature 比较仍按原始 8.2，不看更正值）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "41) 车载读数更正：vr-1 原录 7.0，改为 2.0——同时影响读数时刻在车的全部货物"
curl -sS -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d "{
  \"correction_id\": \"corr-vr-1\",
  \"target_type\": \"vehicle_reading\",
  \"target_id\": \"vr-1\",
  \"expected_version\": 0,
  \"new_temperature\": \"2.0\"
}"; echo

echo "42) 车载原始读数及历次更正可查；车次详情原始温度 7.0 不变、有效温度 2.0/版本 1"
curl -sS "$BASE/vehicle-readings/vr-1/corrections" | python3 -m json.tool; echo
curl -sS "$BASE/trips/$TRIP" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for r in d["readings"]:
    if r["reading_id"] == "vr-1":
        print("vr-1 原始温度 =", r["temperature"],
              "有效温度 =", r["effective_temperature"], "版本 =", r["version"])
'

echo "43) 货物时间线按有效值重建（车载点 temperature=更正值 2.0、version=1）"
curl -sS "$BASE/cargo/$CA/timeline" | python3 -c '
import json, sys
for p in json.load(sys.stdin)["points"]:
    if p["reading_id"] == "vr-1":
        print("vr-1 时间线温度 =", p["temperature"], "版本 =", p["version"])'

echo
echo "================ 本轮新增：装卸前温控兼容性预检与确认 ================"
# 新演示车次：先挂一票上限 4℃ 的货物并录入一笔 7℃ 车载读数，
# 再为“上限 8℃ 的 C-B 拟同车”做装前预检
CTRIP="TRUCK-DEMO-2"; CC1="COLD-PRE-A"; CC2="COLD-PRE-B"
for CC in "$CC1" "$CC2"; do
  LIM="4.0"; [ "$CC" = "$CC2" ] && LIM="8.0"
  curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
    \"cargo_id\": \"$CC\",
    \"started_at\": \"2026-09-30T08:00:00+08:00\",
    \"temp_upper_limit\": \"$LIM\",
    \"initial_holder\": \"北京冷库\"
  }" >/dev/null
done
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$CTRIP\", \"vehicle_no\": \"京B-冷999\", \"note\": \"预检演示班\"
}" >/dev/null
curl -sS -X POST "$BASE/trips/$CTRIP/manifest" -H 'Content-Type: application/json' -d "{
  \"items\": [
    {\"cargo_id\": \"$CC1\",
     \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
     \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
  ]
}" >/dev/null
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-pre-1\", \"trip_id\": \"$CTRIP\",
  \"read_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"7.0\"
}" >/dev/null

echo "44) 查询车次当前修订号（预检需携带预期修订号）"
TREV=$(curl -sS "$BASE/trips/$CTRIP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
echo "    当前车次修订号 = $TREV"

echo "45) 装前预检：拟登记 $CC2（上限 8℃）09:00-18:00 —— 输出各重叠时段的货物集合、"
echo "    温度上限差异、既有车载读数覆盖与潜在越温风险；只读，不写入装卸记录"
PRE=$(curl -sS -X POST "$BASE/trips/$CTRIP/manifest/precheck" \
  -H 'Content-Type: application/json' -d "{
    \"expected_trip_revision\": $TREV,
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }")
echo "$PRE" | python3 -m json.tool
DIGEST=$(echo "$PRE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
echo "    预检摘要 = $DIGEST"
echo "    预检后车次装卸记录不变（仍只有 $CC1）："
curl -sS "$BASE/trips/$CTRIP" | python3 -c '
import json, sys
print("    loadings =", [l["cargo_id"] for l in json.load(sys.stdin)["loadings"]])'

echo "46) 预检后该车次新增一笔车载读数 -> 原摘要过期，确认被拒（409 stale_precheck_digest）"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"vr-pre-2\", \"trip_id\": \"$CTRIP\",
  \"read_at\": \"2026-09-30T11:00:00+08:00\", \"temperature\": \"5.0\"
}" >/dev/null
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"digest\": \"$DIGEST\",
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }"; echo

echo "47) 重新预检（读取最新修订号）并携新摘要确认 -> 201，装卸时段落库"
TREV=$(curl -sS "$BASE/trips/$CTRIP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
DIGEST=$(curl -sS -X POST "$BASE/trips/$CTRIP/manifest/precheck" \
  -H 'Content-Type: application/json' -d "{
    \"expected_trip_revision\": $TREV,
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }" | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"digest\": \"$DIGEST\",
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }"; echo
curl -sS "$BASE/trips/$CTRIP" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("    确认后：revision =", d["revision"],
      "loadings =", [l["cargo_id"] for l in d["loadings"]])'

echo "48) 摘要一次性：同一摘要重复确认 -> 409 stale_precheck_digest（确认已使修订号 +1）"
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"digest\": \"$DIGEST\",
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"}
    ]
  }"; echo

echo "49) 错误示例：预期修订号过期（409 stale_trip_revision）/ 非法时段（422）/ 未知货物（404）"
TREV=$(curl -sS "$BASE/trips/$CTRIP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/precheck" \
  -H 'Content-Type: application/json' -d "{
    \"expected_trip_revision\": 0,
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T19:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T20:00:00+08:00\"}
    ]
  }"; echo
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/precheck" \
  -H 'Content-Type: application/json' -d "{
    \"expected_trip_revision\": $TREV,
    \"items\": [
      {\"cargo_id\": \"$CC2\",
       \"loaded_at\": \"2026-09-30T20:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T19:00:00+08:00\"}
    ]
  }"; echo
curl -sS -X POST "$BASE/trips/$CTRIP/manifest/precheck" \
  -H 'Content-Type: application/json' -d "{
    \"expected_trip_revision\": $TREV,
    \"items\": [
      {\"cargo_id\": \"NO-SUCH-CARGO\",
       \"loaded_at\": \"2026-09-30T19:00:00+08:00\",
       \"unloaded_at\": \"2026-09-30T20:00:00+08:00\"}
    ]
  }"; echo
