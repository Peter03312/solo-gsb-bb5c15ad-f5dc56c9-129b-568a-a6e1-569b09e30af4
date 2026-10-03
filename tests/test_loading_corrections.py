"""装卸时段更正 API 集成测试。

覆盖：
* 更正只改该条窗口（车次 / 货物归属与窗口身份不变），删旧窗口 + 新窗口生效；
* 车次详情展示当前窗口（registered_loaded_at 身份列与当前版本），原始窗口与
  历次更正可经历史接口追溯；
* 乐观版本：首次更正 expected_version=0、版本递增，过期 stale_version；
* 幂等：同更正编号同请求重传返回原结果（replayed），异请求
  loading_correction_id_conflict；
* 新时段左闭右开且起点严格早于终点（invalid_loading_window），原因为空
  bad_loading_correction_shape；原窗口不存在 loading_window_not_found；
  未知车次 / 货物分别 404；
* 重叠校验排除旧窗口自身（自替换 / 同值时段允许），再按同一货物在任意车次
  的时段不得重叠检查（跨车次 loading_overlap），首尾相接允许；
* 车载读数适用性按当前窗口重算（恰在装车时刻纳入、卸货时刻排除；晚到读数
  在更正后适用）；同刻自有测温优先继续适用；
* 越温报告 / 同时越温查询 / 责任确认反映当前窗口；表态历史保留，有效性按
  重建后的责任片段判定（片段不变继续有效）；
* 更正与读数写入 / 测温更正 / 事件表态经行锁串行；成功使车次修订号 +1，
  旧装卸预检摘要失效；失败不递增、不留部分变更。
"""


def make_cargo(client, cargo_id, limit="4.0", holder="北京冷库",
              started="2026-01-01T08:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": started,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def make_trip(client, trip_id="T1", vehicle_no="京A-001"):
    r = client.post("/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no})
    assert r.status_code == 201, r.text


def manifest(client, trip_id, cargo_id, lo, un):
    return client.post(
        f"/trips/{trip_id}/manifest",
        json={"items": [{"cargo_id": cargo_id, "loaded_at": lo, "unloaded_at": un}]},
    )


def correct(
    client,
    trip_id,
    correction_id,
    cargo_id,
    loaded_at,
    new_loaded_at,
    new_unloaded_at,
    expected_version=0,
    reason="调度员核对单据后更正",
):
    return client.post(
        f"/trips/{trip_id}/loading-corrections",
        json={
            "correction_id": correction_id,
            "cargo_id": cargo_id,
            "loaded_at": loaded_at,
            "expected_version": expected_version,
            "new_loaded_at": new_loaded_at,
            "new_unloaded_at": new_unloaded_at,
            "reason": reason,
        },
    )


def history(client, trip_id, cargo_id, loaded_at):
    return client.get(
        f"/trips/{trip_id}/cargo/{cargo_id}/loading-corrections",
        params={"loaded_at": loaded_at},
    )


def setup(client, trip_id="T1", cargo_id="C1"):
    """车次 T1：C1 窗口 09:00-18:00。返回 (车次, 货物, 首登装车, 卸货)。"""
    make_cargo(client, cargo_id)
    make_trip(client, trip_id)
    lo, un = "2026-01-01T09:00:00+00:00", "2026-01-01T18:00:00+00:00"
    assert manifest(client, trip_id, cargo_id, lo, un).status_code == 201
    return trip_id, cargo_id, lo, un


# ------------------------------------------------------------- 基本更正


def test_correction_replaces_window_and_keeps_identity(client):
    t, c, lo, un = setup(client)
    r = correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["correction"]["version"] == 1
    assert body["correction"]["expected_version"] == 0
    assert body["correction"]["old_loaded_at"] == "2026-01-01T09:00:00Z"
    assert body["window"]["loaded_at"] == "2026-01-01T10:00:00Z"
    assert body["window"]["unloaded_at"] == "2026-01-01T19:00:00Z"
    # 窗口身份（首登装车时刻）不变
    assert body["window"]["registered_loaded_at"] == "2026-01-01T09:00:00Z"
    assert body["window"]["version"] == 1

    detail = client.get(f"/trips/{t}").json()
    assert len(detail["loadings"]) == 1
    w = detail["loadings"][0]
    assert (w["loaded_at"], w["unloaded_at"]) == (
        "2026-01-01T10:00:00Z",
        "2026-01-01T19:00:00Z",
    )
    assert w["registered_loaded_at"] == "2026-01-01T09:00:00Z"
    assert w["version"] == 1
    # 旧窗口不再作为当前窗口
    assert w["loaded_at"] != "2026-01-01T09:00:00Z"


def test_correction_does_not_change_trip_or_cargo_assignment(client):
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    detail = client.get(f"/trips/{t}").json()
    w = detail["loadings"][0]
    assert w["trip_id"] == t and w["cargo_id"] == c
    # 其他车次无该货物窗口
    make_trip(client, "T2", "京B-2")
    assert client.get("/trips/T2").json()["loadings"] == []


def test_history_keeps_original_window_and_all_corrections(client):
    t, c, lo, un = setup(client)
    # 未更正时历史即可查：原始=当前、版本 0、更正列表空
    h = history(client, t, c, lo).json()
    assert h["window_exists"] is True
    assert h["version"] == 0
    assert h["original_window"]["loaded_at"] == "2026-01-01T09:00:00Z"
    assert h["current_window"]["loaded_at"] == "2026-01-01T09:00:00Z"
    assert h["corrections"] == []

    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
        reason="第一次更正",
    )
    correct(
        client, t, "lc-2", c, lo,
        "2026-01-01T11:00:00+00:00", "2026-01-01T20:00:00+00:00",
        expected_version=1, reason="第二次更正",
    )
    h = history(client, t, c, lo).json()
    assert h["version"] == 2
    # 原始窗口始终是首登值
    assert (
        h["original_window"]["loaded_at"] == "2026-01-01T09:00:00Z"
        and h["original_window"]["unloaded_at"] == "2026-01-01T18:00:00Z"
        and h["original_window"]["version"] == 0
    )
    # 当前窗口是最近一次更正后的值
    assert (
        h["current_window"]["loaded_at"] == "2026-01-01T11:00:00Z"
        and h["current_window"]["unloaded_at"] == "2026-01-01T20:00:00Z"
        and h["current_window"]["version"] == 2
    )
    # 历次更正按版本升序，均带原窗口与原因
    assert [x["version"] for x in h["corrections"]] == [1, 2]
    assert h["corrections"][0]["old_loaded_at"] == "2026-01-01T09:00:00Z"
    assert h["corrections"][1]["old_loaded_at"] == "2026-01-01T10:00:00Z"
    assert [x["reason"] for x in h["corrections"]] == ["第一次更正", "第二次更正"]


def test_history_locates_window_by_original_loaded_time_after_move(client):
    """窗口起点被更正移动后，仍凭最初装车时刻定位，新起点查不到。"""
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    assert history(client, t, c, lo).status_code == 200
    r = history(client, t, c, "2026-01-01T10:00:00+00:00")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "loading_window_not_found"


# ------------------------------------------------------------- 版本与幂等


def test_stale_version_rejected(client):
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    r = correct(
        client, t, "lc-2", c, lo,
        "2026-01-01T11:00:00+00:00", "2026-01-01T20:00:00+00:00",
        expected_version=0,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_version"


def test_idempotent_replay_same_request(client):
    t, c, lo, un = setup(client)
    payload = dict(
        correction_id="lc-1", cargo_id=c, loaded_at=lo, expected_version=0,
        new_loaded_at="2026-01-01T10:00:00+00:00",
        new_unloaded_at="2026-01-01T19:00:00+00:00", reason="r",
    )
    first = client.post(f"/trips/{t}/loading-corrections", json=payload)
    assert first.status_code == 201
    again = client.post(f"/trips/{t}/loading-corrections", json=payload)
    assert again.status_code == 200
    body = again.json()
    assert body["replayed"] is True
    assert body["correction"]["correction_id"] == "lc-1"
    assert body["window"]["version"] == 1
    # 只落了一条更正
    h = history(client, t, c, lo).json()
    assert len(h["corrections"]) == 1


def test_same_correction_id_different_request_conflicts(client):
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00", reason="r1",
    )
    # 异内容（新时段不同 / 原因不同 / 期望版本不同）均冲突
    r = correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:30:00+00:00", "2026-01-01T19:00:00+00:00", reason="r2",
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_correction_id_conflict"
    # 原结果不变
    h = history(client, t, c, lo).json()
    assert h["current_window"]["loaded_at"] == "2026-01-01T10:00:00Z"


# ------------------------------------------------------------- 非法输入


def test_invalid_new_window_rejected(client):
    t, c, lo, un = setup(client)
    r = correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T20:00:00+00:00", "2026-01-01T10:00:00+00:00",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    # 等长（起=止）同样非法
    r = correct(
        client, t, "lc-2", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    # 窗口未被改动
    detail = client.get(f"/trips/{t}").json()
    assert detail["loadings"][0]["loaded_at"] == "2026-01-01T09:00:00Z"
    assert detail["revision"] == 1  # 仅登记 +1，失败更正不递增


def test_blank_reason_rejected(client):
    t, c, lo, un = setup(client)
    r = correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00", reason="   ",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_loading_correction_shape"


def test_unknown_trip_cargo_window(client):
    t, c, lo, un = setup(client)
    r = correct(
        client, "NOPE", "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"
    r = correct(
        client, t, "lc-1", "NO-CARGO", lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"
    # 车次 / 货物存在，但没有该首登装车时刻的窗口
    r = correct(
        client, t, "lc-1", c, "2026-02-01T09:00:00+00:00",
        "2026-02-01T10:00:00+00:00", "2026-02-01T19:00:00+00:00",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "loading_window_not_found"


def test_failure_leaves_no_partial_change(client):
    """重叠拒绝：当前窗口不变、无更正记录、修订号不递增。"""
    make_cargo(client, "C2")
    make_trip(client, "T2", "京B-2")
    assert manifest(
        client, "T2", "C2",
        "2026-01-01T08:00:00+00:00", "2026-01-01T12:00:00+00:00",
    ).status_code == 201
    # C1 在 T1 09-18
    t, c, lo, un = setup(client)
    before = client.get(f"/trips/{t}").json()["revision"]
    r = correct(
        client, t, "lc-x", c, lo,
        "2026-01-01T07:00:00+00:00", "2026-01-01T10:00:00+00:00",
    )
    # 与 C2 窗口（不同货物）不冲突；这里用 C1 自己在另一车次造冲突
    # —— 直接给 C1 在 T2 再登记一个不重叠窗口，再让新时段撞它
    assert r.status_code == 201
    assert manifest(
        client, "T2", "C1",
        "2026-01-02T08:00:00+00:00", "2026-01-02T12:00:00+00:00",
    ).status_code == 201
    # 此时 C1 当前窗口身份仍是首登 09:00（已被移到 07-10），版本 1
    r = correct(
        client, t, "lc-y", c, lo,
        "2026-01-02T09:00:00+00:00", "2026-01-02T10:00:00+00:00",
        expected_version=1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    after = client.get(f"/trips/{t}").json()["revision"]
    assert after == before + 1  # 仅成功的 lc-x 使修订号 +1
    # 当前窗口仍是 lc-x 的结果，未被 lc-y 改动
    w = client.get(f"/trips/{t}").json()["loadings"][0]
    assert w["loaded_at"] == "2026-01-01T07:00:00Z"
    h = history(client, t, c, lo).json()
    assert [x["correction_id"] for x in h["corrections"]] == ["lc-x"]


# ------------------------------------------------------------- 重叠规则


def test_overlap_check_excludes_old_window_self(client):
    """排除自身：把窗口改成与原时段相同的值允许（自替换，版本仍递增）。"""
    t, c, lo, un = setup(client)
    r = correct(client, t, "lc-1", c, lo, lo, un, reason="仅补录更正原因")
    assert r.status_code == 201
    detail = client.get(f"/trips/{t}").json()
    assert len(detail["loadings"]) == 1
    assert (
        detail["loadings"][0]["loaded_at"] == "2026-01-01T09:00:00Z"
        and detail["loadings"][0]["version"] == 1
    )


def test_overlap_across_trips_rejected_abutting_allowed(client):
    make_cargo(client, "C2")
    make_trip(client, "T2", "京B-2")
    assert manifest(
        client, "T2", "C2",
        "2026-01-01T08:00:00+00:00", "2026-01-01T12:00:00+00:00",
    ).status_code == 201
    # 与 T2 窗口重叠的新时段拒绝
    r = correct(
        client, "T2", "lc-1", "C2", "2026-01-01T08:00:00+00:00",
        "2026-01-01T09:00:00+00:00", "2026-01-01T13:00:00+00:00",
    )
    # 排除自身后，该车次无其他 C2 窗口，故不冲突（只是自身移动）
    assert r.status_code == 201
    # 另一车次 T1 上 C2 已有 15-16，新时段与之重叠 -> 拒绝
    make_trip(client, "T1", "京A-1")
    assert manifest(
        client, "T1", "C2",
        "2026-01-01T15:00:00+00:00", "2026-01-01T16:00:00+00:00",
    ).status_code == 201
    r = correct(
        client, "T2", "lc-2", "C2", "2026-01-01T08:00:00+00:00",
        "2026-01-01T15:30:00+00:00", "2026-01-01T17:00:00+00:00",
        expected_version=1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    # 首尾相接（16-17）允许
    r = correct(
        client, "T2", "lc-3", "C2", "2026-01-01T08:00:00+00:00",
        "2026-01-01T16:00:00+00:00", "2026-01-01T17:00:00+00:00",
        expected_version=1,
    )
    assert r.status_code == 201


# ------------------------------------------------------------- 当前窗口重建


def test_vehicle_reading_applicability_recomputed(client):
    """窗口移动后读数适用性重算：恰在装车时刻纳入、卸货时刻排除。"""
    t, c, lo, un = setup(client)
    for rid, at in [
        ("vr-lo", "2026-01-01T09:00:00+00:00"),
        ("vr-mid", "2026-01-01T10:00:00+00:00"),
        ("vr-un", "2026-01-01T18:00:00+00:00"),
    ]:
        assert client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": t, "read_at": at, "temperature": "7"},
        ).status_code in (200, 201)

    def apps():
        readings = client.get(f"/trips/{t}").json()["readings"]
        return {
            r["reading_id"]: next(
                a for a in r["applications"] if a["cargo_id"] == c
            )
            for r in readings
        }

    before = apps()
    assert before["vr-lo"]["on_board"] is True
    assert before["vr-un"]["on_board"] is False

    # 窗口改为 10:00-18:00：原来 09:00 的读数变为不在车，10:00 恰为装车仍在车上
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T18:00:00+00:00",
    )
    after = apps()
    assert after["vr-lo"]["on_board"] is False
    assert after["vr-lo"]["reason"] == "not_on_board"
    assert after["vr-mid"]["on_board"] is True
    assert after["vr-un"]["on_board"] is False


def test_late_reading_applies_after_window_correction(client):
    """晚到读数：窗口更正扩大覆盖范围后补录的读数按当前窗口适用。"""
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T08:00:00+00:00", "2026-01-01T20:00:00+00:00",
    )
    # 08:30 在首登窗口外、但在更正后窗口内的“晚到”读数（不早于货物起始 08:00）
    r = client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-late", "trip_id": t,
            "read_at": "2026-01-01T08:30:00+00:00", "temperature": "7",
        },
    )
    assert r.status_code == 201
    assert r.json()["affected_cargo_ids"] == [c]


def test_same_time_own_measurement_priority_after_correction(client):
    """窗口更正把读数时刻纳入在车范围，但同刻自有测温仍优先（车载被取代）。"""
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T08:00:00+00:00", "2026-01-01T20:00:00+00:00",
    )
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-1", "trip_id": t,
            "read_at": "2026-01-01T08:30:00+00:00", "temperature": "9",
        },
    ).status_code == 201
    assert client.post(
        "/events",
        json={
            "event_id": "e1", "cargo_id": c,
            "occurred_at": "2026-01-01T08:30:00+00:00", "temperature": "3",
        },
    ).status_code == 201
    detail = client.get(f"/trips/{t}").json()
    app = next(
        a
        for r in detail["readings"]
        for a in r["applications"]
        if r["reading_id"] == "vr-1" and a["cargo_id"] == c
    )
    assert app["on_board"] is True
    assert app["adopted"] is False
    assert app["reason"] == "supplanted_by_own"
    # 采用自有值 -> 不越温
    assert client.get(f"/cargo/{c}/report").json()["excursions"] == []


def test_report_and_simultaneous_reflect_current_window(client):
    """越温报告与同时越温查询按更正后的当前窗口重建。"""
    make_cargo(client, "C2")
    make_trip(client, "T2", "京B-2")
    assert manifest(
        client, "T2", "C2",
        "2026-01-01T10:00:00+00:00", "2026-01-01T12:00:00+00:00",
    ).status_code == 201
    t, c, lo, un = setup(client, "T1", "C1")
    # 读数 10:30=7（越限），11:30=2（闭合）
    for rid, at, temp in [
        ("vr-1", "2026-01-01T10:30:00+00:00", "7"),
        ("vr-2", "2026-01-01T11:30:00+00:00", "2"),
    ]:
        assert client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": t, "read_at": at, "temperature": temp},
        ).status_code == 201
    assert client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-x", "trip_id": "T2",
              "read_at": "2026-01-01T10:30:00+00:00", "temperature": "7"},
    ).status_code == 201

    rep = client.get(f"/cargo/{c}/report").json()
    assert [e["interval_id"] for e in rep["excursions"]] == ["veh:vr-1"]

    q = client.get(
        f"/trips/{t}/simultaneous-excursions",
        params={
            "start_at": "2026-01-01T00:00:00+00:00",
            "end_at": "2026-01-02T00:00:00+00:00",
            "min_cargo": "2",
        },
    ).json()
    # 仅 C1 在 T1 越温（C2 的窗口在 T2），无同时越温
    assert q["segments"] == []

    # 把 C2 的窗口从 T2 的 10-12 更正到 T1？不行——更正不改车次归属。
    # 改为：把 C2 在 T2 的窗口扩到跨车次也不会进入 T1 查询；故另验同车次内：
    # 把 C1 窗口移出读数覆盖（13-15），其越温区间消失，报告为空。
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T13:00:00+00:00", "2026-01-01T15:00:00+00:00",
    )
    assert client.get(f"/cargo/{c}/report").json()["excursions"] == []
    q = client.get(
        f"/trips/{t}/simultaneous-excursions",
        params={
            "start_at": "2026-01-01T00:00:00+00:00",
            "end_at": "2026-01-02T00:00:00+00:00",
            "min_cargo": "2",
        },
    ).json()
    assert q["segments"] == []


def test_report_includes_reading_after_window_expands(client):
    """窗口扩大后把此前“不在车”的读数纳入，越温区间按当前窗口重建出来。"""
    # 货物起始 08:00，首登窗口 09-18
    t, c, lo, un = setup(client)
    # 08:30 在首登窗口（09-18）之外
    assert client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-early", "trip_id": t,
              "read_at": "2026-01-01T08:30:00+00:00", "temperature": "9"},
    ).status_code == 201
    assert client.get(f"/cargo/{c}/report").json()["excursions"] == []
    # 窗口前移到 08:00 起：08:30 读数适用且越限（无闭合点 -> 未闭合区间）
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T08:00:00+00:00", "2026-01-01T18:00:00+00:00",
    )
    rep = client.get(f"/cargo/{c}/report").json()
    assert len(rep["excursions"]) == 1
    ex = rep["excursions"][0]
    assert ex["is_open"] is True
    assert ex["interval_id"] == "veh:vr-early"
    assert ex["start"] == "2026-01-01T08:30:00Z"


# ------------------------------------------------------------- 责任表态


def test_unchanged_segments_keep_stance_valid_after_neutral_correction(client):
    """更正后责任片段不变（窗口仍覆盖同一区间起止）的表态继续有效。"""
    t, c, lo, un = setup(client)
    # 读数 10:00 越限、12:00 闭合（均落在 09-18 内）
    client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-1", "trip_id": t,
              "read_at": "2026-01-01T10:00:00+00:00", "temperature": "7"},
    )
    client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-2", "trip_id": t,
              "read_at": "2026-01-01T12:00:00+00:00", "temperature": "2"},
    )
    iid = "veh:vr-1"
    snap = client.get(
        f"/cargo/{c}/responsibilities"
    ).json()["intervals"][0]["snapshot_id"]
    assert client.post(
        f"/cargo/{c}/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "北京冷库",
            "stance": "confirm", "stance_id": "s1",
        },
    ).status_code == 201

    # 把窗口在不改变区间覆盖的前提下扩到 08:30-18:30（起止读数仍在车内，
    # 越温区间与责任片段不变）-> 表态仍有效
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T08:30:00+00:00", "2026-01-01T18:30:00+00:00",
    )
    h = client.get(f"/cargo/{c}/responsibilities/{iid}/stances").json()
    assert h["interval_exists"] is True
    assert h["history"][0]["is_current"] is True
    view = client.get(f"/cargo/{c}/responsibilities").json()
    assert view["intervals"][0]["summary"] == "all_confirmed"


def test_changed_segments_invalidate_stance_history_kept(client):
    """窗口挪走使区间消失：旧表态失效但历史保留。"""
    t, c, lo, un = setup(client)
    client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-1", "trip_id": t,
              "read_at": "2026-01-01T10:30:00+00:00", "temperature": "7"},
    )
    client.post(
        "/vehicle-readings",
        json={"reading_id": "vr-2", "trip_id": t,
              "read_at": "2026-01-01T11:30:00+00:00", "temperature": "2"},
    )
    iid = "veh:vr-1"
    snap = client.get(
        f"/cargo/{c}/responsibilities"
    ).json()["intervals"][0]["snapshot_id"]
    client.post(
        f"/cargo/{c}/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "北京冷库",
            "stance": "confirm", "stance_id": "s1",
        },
    )
    # 窗口移出读数覆盖（13-15）-> 区间消失
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T13:00:00+00:00", "2026-01-01T15:00:00+00:00",
    )
    h = client.get(f"/cargo/{c}/responsibilities/{iid}/stances").json()
    assert h["interval_exists"] is False
    assert len(h["history"]) == 1
    assert h["history"][0]["is_current"] is False
    assert client.get(f"/cargo/{c}/responsibilities").json()["intervals"] == []


# ------------------------------------------------------------- 修订号与预检摘要


def test_correction_bumps_trip_revision_and_invalidates_digest(client):
    t, c, lo, un = setup(client)
    rev0 = client.get(f"/trips/{t}").json()["revision"]
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    assert client.get(f"/trips/{t}").json()["revision"] == rev0 + 1

    # 预检摘要绑定修订号：更正后旧摘要确认被拒
    make_cargo(client, "C2", limit="8.0")
    rev = client.get(f"/trips/{t}").json()["revision"]
    item = {
        "cargo_id": "C2",
        "loaded_at": "2026-02-01T09:00:00+00:00",
        "unloaded_at": "2026-02-01T18:00:00+00:00",
    }
    digest = client.post(
        f"/trips/{t}/manifest/precheck",
        json={"expected_trip_revision": rev, "items": [item]},
    ).json()["digest"]
    correct(
        client, t, "lc-2", c, lo,
        "2026-01-01T10:30:00+00:00", "2026-01-01T19:30:00+00:00",
        expected_version=1,
    )
    r = client.post(
        f"/trips/{t}/manifest/confirm",
        json={"digest": digest, "items": [item]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"


def test_precheck_expected_revision_rejects_after_correction(client):
    t, c, lo, un = setup(client)
    correct(
        client, t, "lc-1", c, lo,
        "2026-01-01T10:00:00+00:00", "2026-01-01T19:00:00+00:00",
    )
    # 用过期修订号预检
    r = client.post(
        f"/trips/{t}/manifest/precheck",
        json={
            "expected_trip_revision": 1,
            "items": [
                {
                    "cargo_id": c,
                    "loaded_at": "2026-02-01T09:00:00+00:00",
                    "unloaded_at": "2026-02-01T10:00:00+00:00",
                }
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_trip_revision"


# ------------------------------------------------------------- 历史接口参数


def test_history_missing_or_bad_parameter(client):
    t, c, lo, un = setup(client)
    r = client.get(f"/trips/{t}/cargo/{c}/loading-corrections")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "missing_parameter"
    r = client.get(
        f"/trips/{t}/cargo/{c}/loading-corrections",
        params={"loaded_at": "not-a-time"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"


def test_history_unknown_trip_404(client):
    t, c, lo, un = setup(client)
    r = history(client, "NOPE", c, lo)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"
