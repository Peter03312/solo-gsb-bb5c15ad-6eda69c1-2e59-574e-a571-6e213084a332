"""泊位登记 / 开放日历 / 泊位作业规划、确认、批次、作废与改期测试。

规则要点：
* 调度员为现有节点登记泊位；开放日历为互不交叠半开分钟区间，整组替换、
  失败不改旧日历；
* 单船/批次预排可指定终点泊位 + 正整数作业分钟：抵达即开工，作业区间
  完整落在同一开放区间且不与有效确认行程的泊位占用重叠；无解明确报
  NO_FEASIBLE_BERTH_ITINERARY；
* 择优仍是最早抵达（满足泊位条件的最早抵达）+ 同刻航段序列；
* 未指定泊位的旧请求/响应不变（berth_* 字段为 null）；
* 确认在原事务按当时日历与占用复算，行程或作业区间不一致即 PLAN_MISMATCH
  不保存；批次避开前船拟占用，失败全批回滚，同键同内容重放原结果；
* 快照固化泊位作业区间，日历变更不改历史；作废释放泊位占用、历史仍可查；
* 潮位复核仍按原快照；泊位行程的改期建议须同时给出原泊位的即时作业区间；
* 未知节点/泊位、泊位不属终点、非法区间/作业分钟均拒绝。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _setup_two_nodes(client, travel=2, depth=99.0):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=travel, depth=depth)


def _berth(client, node="B", name=None, calendar=None):
    body = {"name": name}
    if calendar is not None:
        body["calendar"] = calendar
    r = client.post(f"/api/nodes/{node}/berths", json=body)
    assert r.status_code == 201, r.text
    return r.json()["berth_id"]


def _plan(client, destination="B", berth=None, job=None, start=0, window=40,
          origin="A", rev=None):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
    }
    if berth is not None:
        body["berth_id"] = berth
        body["job_minutes"] = job
    if rev is not None:
        body["revision"] = rev
    return client.post("/api/trips/plan", json=body)


def _confirm_from_plan(client, plan):
    body = {k: plan[k] for k in (
        "origin", "destination", "start_minute", "window_minutes",
        "draft", "height", "margin", "revision", "arrival_minute",
        "total_wait_minutes", "itinerary")}
    if plan.get("berth_id") is not None:
        body["berth_id"] = plan["berth_id"]
        body["job_minutes"] = plan["job_minutes"]
    return client.post("/api/trips/confirm", json=body)


# ---- 1. 泊位登记与开放日历 --------------------------------------------------------

def test_register_berth_with_calendar(client):
    add_node(client, "B")
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 100},
                                   {"start_minute": 200, "end_minute": 300}])
    r = client.get(f"/api/berths/{bid}")
    assert r.status_code == 200
    data = r.json()
    assert data["node_id"] == "B"
    assert data["calendar"] == [
        {"start_minute": 0, "end_minute": 100},
        {"start_minute": 200, "end_minute": 300},
    ]
    assert client.get("/api/berths").json() == [data]


def test_register_berth_unknown_node(client):
    r = client.post("/api/nodes/ZZ/berths", json={"calendar": []})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NODE_NOT_FOUND"


def test_register_berth_invalid_and_overlapping_interval(client):
    add_node(client, "B")
    r = client.post("/api/nodes/B/berths",
                    json={"calendar": [{"start_minute": -1, "end_minute": 10}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_BERTH_INTERVAL"

    r = client.post("/api/nodes/B/berths",
                    json={"calendar": [{"start_minute": 10, "end_minute": 5}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_BERTH_INTERVAL"

    r = client.post("/api/nodes/B/berths",
                    json={"calendar": [{"start_minute": 0, "end_minute": 10},
                                       {"start_minute": 5, "end_minute": 20}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_BERTH_INTERVAL"
    # 失败不留半成品泊位
    assert client.get("/api/berths").json() == []

    # 首尾相接合法
    r = client.post("/api/nodes/B/berths",
                    json={"calendar": [{"start_minute": 0, "end_minute": 10},
                                       {"start_minute": 10, "end_minute": 20}]})
    assert r.status_code == 201, r.text


def test_replace_calendar_failure_keeps_old(client):
    add_node(client, "B")
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 100}])

    r = client.put(f"/api/berths/{bid}/calendar",
                   json={"calendar": [{"start_minute": 0, "end_minute": -5}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_BERTH_INTERVAL"

    r = client.put(f"/api/berths/{bid}/calendar",
                   json={"calendar": [{"start_minute": 0, "end_minute": 10},
                                      {"start_minute": 9, "end_minute": 20}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_BERTH_INTERVAL"

    # 旧日历原样保留
    data = client.get(f"/api/berths/{bid}").json()
    assert data["calendar"] == [{"start_minute": 0, "end_minute": 100}]

    # 空列表整组替换 -> 全天不开放
    r = client.put(f"/api/berths/{bid}/calendar", json={"calendar": []})
    assert r.status_code == 200
    assert r.json()["calendar"] == []


def test_get_unknown_berth_and_replace_unknown(client):
    r = client.get("/api/berths/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BERTH_NOT_FOUND"
    r = client.put("/api/berths/999/calendar", json={"calendar": []})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BERTH_NOT_FOUND"


# ---- 2. 泊位感知规划：抵达即开工 ---------------------------------------------------

def test_plan_with_berth_arrive_and_work_immediately(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])

    r = _plan(client, berth=bid, job=5, window=40, rev=rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["arrival_minute"] == 2
    assert data["berth_id"] == bid
    assert data["job_minutes"] == 5
    assert data["job_start_minute"] == 2          # 抵达即开工
    assert data["job_end_minute"] == 7


def test_plan_without_berth_response_fields_null(client):
    _setup_two_nodes(client)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    r = _plan(client, rev=rev)
    assert r.status_code == 200
    data = r.json()
    assert data["berth_id"] is None
    assert data["job_minutes"] is None
    assert data["job_start_minute"] is None
    assert data["job_end_minute"] is None


def test_plan_berth_unknown_and_not_at_destination(client):
    add_node(client, "A"); add_node(client, "B"); add_node(client, "C")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid_b = _berth(client, node="B", calendar=[{"start_minute": 0, "end_minute": 100}])

    # 未知泊位
    r = _plan(client, destination="B", berth=999, job=5, rev=rev)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BERTH_NOT_FOUND"

    # 泊位不属终点（B 的泊位用于终点 C）
    r = _plan(client, destination="C", berth=bid_b, job=5, rev=rev)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "BERTH_NOT_AT_DESTINATION"


def test_plan_berth_coupling_and_nonpositive_job_rejected(client):
    _setup_two_nodes(client)
    seed_initial_revision(_level(0, 100, 3.0))
    base = {"origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 40, "draft": 1.0, "height": 0.0, "margin": 0.0}
    r = client.post("/api/trips/plan", json={**base, "berth_id": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post("/api/trips/plan", json={**base, "job_minutes": 5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post("/api/trips/plan", json={**base, "berth_id": 1, "job_minutes": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_plan_berth_job_must_fit_single_open_window(client):
    # 作业 6 分钟；日历 [0,5) 放不下，[5,20) 可放 -> 船上游等待、5 抵达开工
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 5},
                                   {"start_minute": 5, "end_minute": 20}])
    r = _plan(client, berth=bid, job=6, window=40, rev=rev)
    assert r.status_code == 200, r.text
    assert [l["start_minute"] for l in r.json()["itinerary"]] == [3]
    assert r.json()["job_start_minute"] == 5

    # 作业 [2,5) 恰好 3 分钟：2 抵达即开工，开放区间首尾相接处合法
    r = _plan(client, berth=bid, job=3, window=40, rev=rev)
    assert r.status_code == 200
    assert r.json()["job_start_minute"] == 2
    assert r.json()["job_end_minute"] == 5

    # 作业 20 分钟：任何单个开放区间都放不下 -> 明确无解（不许跨区间拼接）
    r = _plan(client, berth=bid, job=20, window=40, rev=rev)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_BERTH_ITINERARY"


def test_plan_berth_closed_calendar_no_solution(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[])  # 全天不开放
    r = _plan(client, berth=bid, job=5, window=40, rev=rev)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_BERTH_ITINERARY"


def test_plan_berth_avoids_confirmed_occupancy_half_open(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])

    first = _plan(client, berth=bid, job=5, window=40, rev=rev).json()
    assert _confirm_from_plan(client, first).status_code == 201

    # 已确认作业 [2,7)；第二艘作业 3 分钟：最早可开工 = 7（半开，7 不重叠）
    second = _plan(client, berth=bid, job=3, window=40, rev=rev)
    assert second.status_code == 200
    assert second.json()["job_start_minute"] == 7
    assert second.json()["job_end_minute"] == 10

    # 作业区间不得与占用重叠：6 分钟作业无法在 7 之前开工（2 撞 [2,7)），
    # 最早可开工 7 -> [7,13)
    third = _plan(client, berth=bid, job=6, window=40, rev=rev)
    assert third.status_code == 200
    assert third.json()["job_start_minute"] == 7


def test_plan_berth_window_only_bounds_sailing_not_job(client):
    # 窗口 4 分钟：0 进入、2 抵达仍在窗口内；作业 10 分钟延伸到 12 合法
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    r = _plan(client, berth=bid, job=10, window=4, rev=rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["arrival_minute"] == 2
    assert data["job_end_minute"] == 12

    # 窗口 1 分钟：最早 0 进入、2 抵达越出窗口 -> 无解
    r = _plan(client, berth=bid, job=10, window=1, rev=rev)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_BERTH_ITINERARY"


def test_plan_berth_waiting_upstream_for_open_window(client):
    # 航段 A->B 航时 2，泊位 [10, 20) 开放、作业 3：船在 A 等待到 8 出发，
    # 10 抵达即开工（不能 2 抵达后在泊位等待）
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 10, "end_minute": 20}])
    r = _plan(client, berth=bid, job=3, window=40, rev=rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [l["start_minute"] for l in data["itinerary"]] == [8]
    assert data["arrival_minute"] == 10
    assert data["job_start_minute"] == 10
    assert data["job_end_minute"] == 13
    # 总等待 = 8 分钟（在上游 A 等待，不在泊位等待）
    assert data["total_wait_minutes"] == 8


# ---- 3. 泊位行程确认：事务复算 + 快照固化 ------------------------------------------

def test_confirm_berth_trip_persists_snapshot(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, berth=bid, job=5, rev=rev).json()

    r = _confirm_from_plan(client, plan)
    assert r.status_code == 201, r.text
    trip = r.json()
    assert trip["berth_id"] == bid
    assert trip["berth_job_start"] == 2
    assert trip["berth_job_end"] == 7

    # 列表接口同样带固化字段
    listed = client.get("/api/trips").json()
    assert listed[0]["berth_id"] == bid
    assert listed[0]["berth_job_start"] == 2
    assert listed[0]["berth_job_end"] == 7


def test_confirm_rejected_after_calendar_change_keeps_old(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, berth=bid, job=5, rev=rev).json()

    # 日历改为 [0,2) 即关闭：抵达分钟 2 已不开放
    assert client.put(
        f"/api/berths/{bid}/calendar",
        json={"calendar": [{"start_minute": 0, "end_minute": 2}]}).status_code == 200

    r = _confirm_from_plan(client, plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []

    # 恢复日历后确认成功
    assert client.put(
        f"/api/berths/{bid}/calendar",
        json={"calendar": [{"start_minute": 0, "end_minute": 1440}]}).status_code == 200
    assert _confirm_from_plan(client, plan).status_code == 201


def test_confirmed_berth_snapshot_survives_calendar_change(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, berth=bid, job=5, rev=rev).json()
    assert _confirm_from_plan(client, plan).status_code == 201

    # 日历变更不改历史：快照作业区间仍是 [2,7)，且仍占用这些分钟
    client.put(f"/api/berths/{bid}/calendar", json={"calendar": []})
    trips = client.get("/api/trips").json()
    assert trips[0]["berth_job_start"] == 2
    assert trips[0]["berth_job_end"] == 7

    # 固化占用仍封闭：日历恢复后新船最早可开工仍是 7
    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 0, "end_minute": 1440}]})
    again = _plan(client, berth=bid, job=3, rev=rev)
    assert again.json()["job_start_minute"] == 7


# ---- 4. 批次：前船拟泊位占用、全批回滚、幂等重放 -----------------------------------

def _batch_plan(client, ships, rev):
    return client.post("/api/trips/batch/plan",
                       json={"ships": ships, "revision": rev})


def _batch_confirm(client, plan, key):
    ships = []
    for s in plan["ships"]:
        item = {k: s[k] for k in (
            "origin", "destination", "start_minute", "window_minutes",
            "draft", "height", "margin", "itinerary")}
        if s.get("berth_id") is not None:
            item["berth_id"] = s["berth_id"]
            item["job_minutes"] = s["job_minutes"]
        ships.append(item)
    return client.post("/api/trips/batch/confirm",
                       json={"ships": ships, "revision": plan["revision"],
                             "idempotency_key": key})


def _bship(berth=None, job=None, start=0, window=40):
    item = {"origin": "A", "destination": "B", "start_minute": start,
            "window_minutes": window, "draft": 1.0, "height": 0.0, "margin": 0.0}
    if berth is not None:
        item["berth_id"] = berth
        item["job_minutes"] = job
    return item


def test_batch_plan_berth_ships_avoid_preceding(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])

    r = _batch_plan(client, [_bship(bid, 5), _bship(bid, 3)], rev)
    assert r.status_code == 200, r.text
    ships = r.json()["ships"]
    # 前船作业 [2,7)；后船半开衔接最早 7 开工（5 抵达、上游等待到 7 出发？
    # 航时 2：须 5 出发 7 抵达）
    assert ships[0]["job_start_minute"] == 2
    assert ships[0]["job_end_minute"] == 7
    assert ships[1]["job_start_minute"] == 7
    assert ships[1]["job_end_minute"] == 10
    assert [l["start_minute"] for l in ships[1]["itinerary"]] == [5]
    # 预排不占位
    assert client.get("/api/trips").json() == []


def test_batch_plan_berth_no_solution_has_ship_index(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 4}])
    # 船 0：作业 [2,4) 恰好；船 1：作业 3 已无任何开放区间可放下
    r = _batch_plan(client, [_bship(bid, 2), _bship(bid, 3, window=10)], rev)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_BERTH_ITINERARY"
    assert err["details"]["ship_index"] == 1


def test_batch_confirm_rolls_back_on_calendar_change(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    plan = _batch_plan(client, [_bship(bid, 5), _bship(bid, 3)], rev).json()

    # 把日历缩短到使后船作业无解：前船 [2,7) 本身就越界 [0,6)
    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 0, "end_minute": 6}]})
    r = _batch_confirm(client, plan, "key-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert r.json()["error"]["details"]["ship_index"] == 0
    assert client.get("/api/trips").json() == []

    # 恢复后成功
    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 0, "end_minute": 1440}]})
    r = _batch_confirm(client, plan, "key-1")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["replayed"] is False
    assert [t["job_start_minute"] for t in data["trips"]] == [2, 7]


def test_batch_confirm_idempotent_replay(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    plan = _batch_plan(client, [_bship(bid, 5), _bship(bid, 3)], rev).json()

    first = _batch_confirm(client, plan, "key-x").json()
    # 同键同内容重放仍返回首次结果（含泊位作业区间、行程号）
    r = _batch_confirm(client, plan, "key-x")
    assert r.status_code == 200
    assert r.json()["trips"] == first["trips"]
    assert r.json()["replayed"] is True

    # 日历再怎么变，同键同内容重放仍返回首次结果，且不新增行程
    client.put(f"/api/berths/{bid}/calendar", json={"calendar": []})
    r = _batch_confirm(client, plan, "key-x")
    assert r.status_code == 200
    replay = r.json()
    assert replay["replayed"] is True
    assert replay["trips"] == first["trips"]
    assert len(client.get("/api/trips").json()) == 2

    # 同键异内容（改作业分钟）-> 冲突（日历恢复后才能重新预排，哈希仍不同）
    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 0, "end_minute": 1440}]})
    plan2 = _batch_plan(client, [_bship(bid, 4), _bship(bid, 3)], rev).json()
    r = _batch_confirm(client, plan2, "key-x")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_KEY_CONFLICT"


# ---- 5. 作废释放泊位占用；历史仍可查 -----------------------------------------------

def test_void_releases_berth_occupancy(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])

    first = _confirm_from_plan(
        client, _plan(client, berth=bid, job=5, rev=rev).json()).json()
    # 作废前：第二艘最早 7 开工
    assert _plan(client, berth=bid, job=3, rev=rev).json()["job_start_minute"] == 7

    r = client.post(f"/api/trips/{first['trip_id']}/void",
                    json={"reason": "误确认"})
    assert r.status_code == 201

    # 作废后：占用释放，第二艘可 2 开工
    assert _plan(client, berth=bid, job=3, rev=rev).json()["job_start_minute"] == 2

    # 历史快照仍原样可查
    got = client.get(f"/api/trips/{first['trip_id']}").json()
    assert got["voided_at"] is not None
    assert got["berth_job_start"] == 2
    assert got["berth_job_end"] == 7


# ---- 6. 复核仍按原快照；泊位改期建议含即时作业区间 ----------------------------------

def test_review_berth_trip_safe_then_unsafe_with_berth_reschedule(client):
    # A->B 航时 20、静水深度 0.5：潮位 3 时吃水 1 安全，潮位 0 时不足。
    _setup_two_nodes(client, travel=20, depth=0.5)
    rev = seed_initial_revision(_level(0, 200, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    trip = _confirm_from_plan(
        client, _plan(client, berth=bid, job=5, window=120, rev=rev).json()).json()
    assert trip["arrival_minute"] == 20

    # 潮位安全：safe=true，无改期（泊位日历不影响潮位复核）
    safe = client.post(f"/api/trips/{trip['trip_id']}/review").json()
    assert safe["safe"] is True
    assert safe["reschedule"] is None

    # 潮位更正：让航段在 0..59 分钟水深不足（名义潮位 0，要求吃水 1）
    conn = connect()
    new_rev = conn.execute(
        "INSERT INTO tide_revisions(note) VALUES ('低潮更正')").lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level, error)"
        f" SELECT {new_rev}, minute, level, error FROM tide_values WHERE revision = {rev}"
    )
    conn.executemany(
        "UPDATE tide_values SET level = ? WHERE revision = ? AND minute <= ?",
        [(0.0, new_rev, 59)],
    )
    conn.commit()
    conn.close()

    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    json={"target_revision": new_rev})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["safe"] is False
    resched = data["reschedule"]
    assert resched is not None
    # 改期建议必须同时给出原泊位可用的即时作业区间（只建议不预留）
    assert resched["berth_id"] == bid
    assert resched["job_start_minute"] == resched["arrival_minute"]
    assert resched["job_end_minute"] == resched["arrival_minute"] + 5
    # 建议不占位：再次复核结果一致
    again = client.post(f"/api/trips/{trip['trip_id']}/review",
                        json={"target_revision": new_rev}).json()
    assert again["reschedule"]["itinerary"] == resched["itinerary"]


def test_review_berth_reschedule_respects_current_calendar(client):
    # 原行程在新潮位下可改期到 60 抵达；若当时泊位日历只在 80 之后开放，
    # 改期建议须继续在上游等待，80 抵达即开工。
    _setup_two_nodes(client, travel=20, depth=0.5)
    rev = seed_initial_revision(_level(0, 200, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    trip = _confirm_from_plan(
        client, _plan(client, berth=bid, job=5, window=120, rev=rev).json()).json()

    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 80, "end_minute": 200}]})

    conn = connect()
    new_rev = conn.execute(
        "INSERT INTO tide_revisions(note) VALUES ('低潮')").lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level, error)"
        f" SELECT {new_rev}, minute, level, error FROM tide_values WHERE revision = {rev}"
    )
    conn.execute(
        f"UPDATE tide_values SET level = 0 WHERE revision = {new_rev} AND minute <= 59"
    )
    conn.commit()
    conn.close()

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": new_rev}).json()
    assert data["safe"] is False
    resched = data["reschedule"]
    assert resched is not None
    assert resched["arrival_minute"] == 80
    assert resched["job_start_minute"] == 80
    assert resched["job_end_minute"] == 85


def test_review_berth_reschedule_no_solution_when_calendar_blocks(client):
    _setup_two_nodes(client, travel=20, depth=0.5)
    rev = seed_initial_revision(_level(0, 200, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    trip = _confirm_from_plan(
        client, _plan(client, berth=bid, job=5, window=120, rev=rev).json()).json()

    # 潮位 0..59 不可航；日历只在 30 之前开放 -> 潮位可行的抵达（>=60）无泊位可用
    client.put(f"/api/berths/{bid}/calendar",
               json={"calendar": [{"start_minute": 0, "end_minute": 30}]})
    conn = connect()
    new_rev = conn.execute(
        "INSERT INTO tide_revisions(note) VALUES ('低潮')").lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level, error)"
        f" SELECT {new_rev}, minute, level, error FROM tide_values WHERE revision = {rev}"
    )
    conn.execute(
        f"UPDATE tide_values SET level = 0 WHERE revision = {new_rev} AND minute <= 59"
    )
    conn.commit()
    conn.close()

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": new_rev}).json()
    assert data["safe"] is False
    assert data["reschedule"] is None
    assert data["no_solution"] is True


def test_batch_mixed_berth_and_non_berth_ships(client):
    _setup_two_nodes(client, travel=2)
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])

    # 船 0 非泊位（只占航段），船 1 泊位作业；后船同时避开前船航段与泊位拟占用
    r = _batch_plan(client, [_bship(), _bship(bid, 3)], rev)
    assert r.status_code == 200, r.text
    ships = r.json()["ships"]
    assert ships[0]["berth_id"] is None
    assert [l["start_minute"] for l in ships[0]["itinerary"]] == [0]
    # 船 1 避开船 0 的航段占用 0,1：2 出发、4 抵达，4 开工
    assert [l["start_minute"] for l in ships[1]["itinerary"]] == [2]
    assert ships[1]["job_start_minute"] == 4


def test_review_berth_reschedule_avoids_other_current_occupancy(client):
    # 被复核行程须改期：航时 20，潮位 0..59 不足，最早 60 出发、80 抵达。
    # 另一有效确认行程占着泊位 [60,90)，则即时作业（5 分钟）最早 90 抵达开工
    # （在 80 抵达开工将与 [60,90) 重叠）。
    _setup_two_nodes(client, travel=20, depth=0.5)
    rev = seed_initial_revision(_level(0, 300, 3.0))
    bid = _berth(client, calendar=[{"start_minute": 0, "end_minute": 1440}])
    trip = _confirm_from_plan(
        client, _plan(client, berth=bid, job=5, window=120, rev=rev).json()).json()

    # 另一艘船直接固化为泊位 [60,90) 的作业占用（零航段快照：无航段占用，
    # 泊位占用仅以固化的 berth_job_* 三列计）
    conn = connect()
    conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary,"
        "  berth_id, berth_job_start, berth_job_end)"
        " VALUES (?, 'B', 'B', 60, 200, 1.0, 0.0, 0.0, 60, ?, ?, 60, 90)",
        (rev, json.dumps([]), bid),
    )
    conn.commit()
    conn.close()

    # 潮位 0..59 不足，迫使被复核行程改期（最早 60 出发、80 抵达）
    conn = connect()
    new_rev = conn.execute(
        "INSERT INTO tide_revisions(note) VALUES ('低潮')").lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level, error)"
        f" SELECT {new_rev}, minute, level, error FROM tide_values WHERE revision = {rev}"
    )
    conn.execute(
        f"UPDATE tide_values SET level = 0 WHERE revision = {new_rev} AND minute <= 59"
    )
    conn.commit()
    conn.close()

    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": new_rev}).json()
    assert data["safe"] is False
    resched = data["reschedule"]
    assert resched is not None
    # 60..89 被占，80 抵达开工 [80,85) 与占用重叠 -> 上游等待、90 抵达开工
    assert resched["arrival_minute"] == 90
    assert resched["job_start_minute"] == 90
    assert resched["job_end_minute"] == 95


# ---- 7. 零航段泊位（起终点相同）----------------------------------------------------
def test_zero_leg_berth_plan(client):
    add_node(client, "A")
    rev = seed_initial_revision(_level(0, 100, 3.0))
    bid = _berth(client, node="A", calendar=[{"start_minute": 10, "end_minute": 30}])
    body = {"origin": "A", "destination": "A", "start_minute": 10,
            "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
            "berth_id": bid, "job_minutes": 5}
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["itinerary"] == []
    assert r.json()["job_start_minute"] == 10
    assert r.json()["job_end_minute"] == 15

    # 起始分钟泊位未开放 -> 无解（零航段不能等待）
    body["start_minute"] = 5
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_BERTH_ITINERARY"
