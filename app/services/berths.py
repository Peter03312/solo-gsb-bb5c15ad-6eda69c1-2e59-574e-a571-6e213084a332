"""泊位登记、开放日历与泊位作业占用。

调度员为**现有节点**登记泊位（berth），并按泊位维护一组**互不交叠的半开
分钟区间** ``[start_minute, end_minute)`` 作为开放日历：

* **登记**（``POST /api/nodes/{node_id}/berths``）：泊位挂在唯一终点节点下；
  可在建泊位时同时给出初始日历（空/省略表示初始不开放）。未知节点
  ``NODE_NOT_FOUND``；初始日历非法 ``INVALID_BERTH_INTERVAL``、组内交叠
  ``OVERLAPPING_BERTH_INTERVAL``，任一失败不留半成品泊位。
* **整组替换日历**（``PUT /api/berths/{berth_id}/calendar``）：以提交清单
  整体替换该泊位旧日历（空列表 ``[]`` 表示此后全天不开放）；未知泊位
  ``BERTH_NOT_FOUND``、非法区间/组内交叠同样拒绝且**旧日历原样保留**
  （单事务先全删后插，失败回滚）。

泊位规划语义（单船与批次的预排/确认共用，见 ``plan_with_berth``）：

* 请求须同时给出 ``berth_id`` 与正整数 ``job_minutes``（成对出现，
  schema 层校验），且泊位必须属于终点节点，否则 ``BERTH_NOT_AT_DESTINATION``；
* **抵达即开工**：船在某整分钟抵达泊位节点后立即作业，作业区间
  ``[arrival, arrival+job_minutes)`` 必须**完整落在同一开放区间**内
  （不允许跨日历区间拼接、不允许在泊位等待），且不与**有效确认行程**
  （已作废排除）的泊位作业区间重叠；半开口径下后船可在前船作业止点开工；
* 择优口径不变：最早抵达（此处指满足泊位条件的最早抵达）+ 同刻航段序列
  字典序；窗口内只有航行可行但无满足泊位条件的抵达 ->
  ``NO_FEASIBLE_BERTH_ITINERARY``。

确认快照固化泊位作业区间（``confirmed_trips`` 的
``berth_id/berth_job_start/berth_job_end`` 三列）：之后日历如何变更都不改写
历史，泊位占用读取只统计未作废行程的固化区间；作废即释放后续占用，历史
仍可查。
"""
import sqlite3
from typing import Optional

from ..errors import ApiError, no_berth_itinerary, unknown_berth, berth_not_at_destination
from .planning import find_itinerary


# ---- 读取 -----------------------------------------------------------------------

def get_berth(conn, berth_id: int) -> dict:
    """读取泊位及其开放日历（按起点升序）。未知泊位 -> 404 BERTH_NOT_FOUND。"""
    row = conn.execute(
        "SELECT berth_id, node_id, name, created_at FROM berths WHERE berth_id = ?",
        (berth_id,),
    ).fetchone()
    if row is None:
        raise unknown_berth(berth_id)
    return {
        "berth_id": row["berth_id"],
        "node_id": row["node_id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "calendar": [
            {"start_minute": s, "end_minute": e}
            for s, e in load_calendar(conn, berth_id)
        ],
    }


def list_berths(conn) -> list[dict]:
    """列出全部泊位（按泊位编号升序），各附开放日历（按起点升序）。"""
    rows = conn.execute(
        "SELECT berth_id FROM berths ORDER BY berth_id"
    ).fetchall()
    return [get_berth(conn, r["berth_id"]) for r in rows]


def load_calendar(conn, berth_id: int) -> list[tuple[int, int]]:
    """读取泊位开放日历，返回按起点升序、互不交叠的半开区间 [(start, end), ...]。"""
    rows = conn.execute(
        "SELECT start_minute, end_minute FROM berth_calendar"
        " WHERE berth_id = ? ORDER BY start_minute",
        (berth_id,),
    ).fetchall()
    return [(r["start_minute"], r["end_minute"]) for r in rows]


# ---- 校验 / 写入 -----------------------------------------------------------------

def validate_calendar(items) -> list[tuple[int, int]]:
    """校验提交的开放日历，返回规范化后的升序区间列表（可为空）。

    非法区间 -> 422 INVALID_BERTH_INTERVAL；
    组内半开区间交叠 -> 422 OVERLAPPING_BERTH_INTERVAL（首尾相接合法）。
    """
    intervals: list[tuple[int, int]] = []
    for idx, it in enumerate(items):
        start = it["start_minute"]
        end = it["end_minute"]
        if start < 0 or end <= start:
            raise ApiError(
                422,
                "INVALID_BERTH_INTERVAL",
                f"第 {idx} 个泊位开放区间非法：要求 start_minute>=0 且"
                " end_minute>start_minute",
                {"index": idx, "start_minute": start, "end_minute": end},
            )
        intervals.append((start, end))

    intervals.sort(key=lambda x: x[0])
    for i in range(1, len(intervals)):
        prev_start, prev_end = intervals[i - 1]
        start, end = intervals[i]
        if start < prev_end:
            raise ApiError(
                422,
                "OVERLAPPING_BERTH_INTERVAL",
                f"泊位开放区间组内交叠：[{prev_start}, {prev_end}) 与 "
                f"[{start}, {end}) 相互重叠，拒绝替换",
                {"previous": {"start_minute": prev_start, "end_minute": prev_end},
                 "current": {"start_minute": start, "end_minute": end}},
            )
    return intervals


def create_berth(conn, node_id: str, name: Optional[str], items) -> dict:
    """在现有节点下登记泊位并写入初始日历。

    未知节点 -> 404 NODE_NOT_FOUND；初始日历非法/交叠 -> 422 且不留半成品。
    """
    if conn.execute(
        "SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)
    ).fetchone() is None:
        from ..errors import unknown_node
        raise unknown_node(node_id)

    intervals = validate_calendar(items)

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO berths(node_id, name) VALUES (?, ?)",
            (node_id, name),
        )
        berth_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO berth_calendar(berth_id, start_minute, end_minute)"
            " VALUES (?, ?, ?)",
            [(berth_id, s, e) for s, e in intervals],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_berth(conn, berth_id)


def replace_calendar(conn, berth_id: int, items) -> dict:
    """整组替换泊位开放日历（单事务，失败不改旧日历）。

    未知泊位 -> 404 BERTH_NOT_FOUND；空组 -> 删除全部旧日历（全天不开放）。
    """
    if conn.execute(
        "SELECT 1 FROM berths WHERE berth_id = ?", (berth_id,)
    ).fetchone() is None:
        raise unknown_berth(berth_id)

    intervals = validate_calendar(items)

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM berth_calendar WHERE berth_id = ?", (berth_id,)
        )
        conn.executemany(
            "INSERT INTO berth_calendar(berth_id, start_minute, end_minute)"
            " VALUES (?, ?, ?)",
            [(berth_id, s, e) for s, e in intervals],
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    return get_berth(conn, berth_id)


# ---- 泊位作业占用 -----------------------------------------------------------------

def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并半开/闭边界混合的整数分钟区间集合为升序互不交叠的闭区间列表。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[int]] = []
    cs, ce = ordered[0][0], ordered[0][1] - 1
    for s, e in ordered[1:]:
        if s <= ce + 1:  # 半开口径下首尾相接（前船 job_end == 后船 job_start）也合并
            ce = max(ce, e - 1)
        else:
            merged.append([cs, ce])
            cs, ce = s, e - 1
    merged.append([cs, ce])
    return [(s, e) for s, e in merged]


def load_berth_occupancy(conn, berth_id: int,
                         exclude_trip_id: Optional[int] = None
                         ) -> list[tuple[int, int]]:
    """读取某泊位上全部**有效**确认行程的作业占用（已作废排除）。

    返回按分钟升序、互不交叠（首尾相接已合并）的闭区间列表
    [(start_minute, end_minute_inclusive), ...]，即固化的作业区间
    [berth_job_start, berth_job_end) 占用的全部分钟。确认快照固化后即使用
    开放日历变更也不改写——这里只按固化区间统计。
    """
    sql = (
        "SELECT c.berth_job_start AS s, c.berth_job_end AS e"
        " FROM confirmed_trips c"
        " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
        " WHERE c.berth_id = ? AND v.trip_id IS NULL"
    )
    params: list = [berth_id]
    if exclude_trip_id is not None:
        sql += " AND c.trip_id <> ?"
        params.append(exclude_trip_id)
    rows = conn.execute(sql, params).fetchall()
    return _merge([(r["s"], r["e"]) for r in rows])


def feasible_job_starts(calendar: list[tuple[int, int]],
                        occupied: list[tuple[int, int]],
                        job_minutes: int,
                        lo: int, hi: int,
                        extra_forbidden: Optional[list[tuple[int, int]]] = None
                        ) -> set[int]:
    """求候选抵达分钟集合：作业 [a, a+job) 完整落在同一开放区间且不撞占用。

    候选范围为闭区间 [lo, hi]（作业可延伸到 hi 之后——窗口只约束航行，不
    约束作业时长）。calendar 为半开开放区间；occupied 为已合并的**闭**占用
    分钟区间（见 load_berth_occupancy）；extra_forbidden 为批次前船拟占用的
    **半开**区间 (start, end) 列表。
    """
    # 统一为闭分钟区间后合并：半开 [s, e) -> 闭 [s, e-1]
    forbidden = list(occupied)
    if extra_forbidden:
        forbidden = _merge(
            [(s, e + 1) for s, e in occupied] + list(extra_forbidden)
        )

    blocked: set[int] = set()
    for fs, fe in forbidden:  # 闭区间 [fs, fe]
        if fs <= hi and fe >= lo:
            blocked.update(range(max(lo, fs), min(hi, fe) + 1))

    starts: set[int] = set()
    for os_, oe in calendar:  # 开放区间 [os_, oe)
        # a >= os_ 且 a+job <= oe
        clo = max(lo, os_)
        chi = min(hi, oe - job_minutes)
        if clo > chi:
            continue
        if not blocked:
            starts.update(range(clo, chi + 1))
        else:
            starts.update(a for a in range(clo, chi + 1) if a not in blocked)
    return starts


# ---- 泊位感知规划 -----------------------------------------------------------------

def resolve_berth(conn, destination: str, berth_id: int) -> dict:
    """校验泊位存在且属于终点节点。

    未知泊位 -> 404 BERTH_NOT_FOUND；泊位不属终点 ->
    422 BERTH_NOT_AT_DESTINATION。返回 {"berth_id", "node_id", "name"}。
    """
    row = conn.execute(
        "SELECT berth_id, node_id, name FROM berths WHERE berth_id = ?",
        (berth_id,),
    ).fetchone()
    if row is None:
        raise unknown_berth(berth_id)
    if row["node_id"] != destination:
        raise berth_not_at_destination(berth_id, row["node_id"], destination)
    return {"berth_id": row["berth_id"], "node_id": row["node_id"],
            "name": row["name"]}


def plan_with_berth(conn, *, berth_id: int, job_minutes: int,
                    extra_berth_occupancy: Optional[list[tuple[int, int]]] = None,
                    exclude_trip_id: Optional[int] = None,
                    **kwargs) -> dict:
    """泊位感知的最早抵达规划（预排与确认复算共用）。

    kwargs 与 find_itinerary 一致（origin/destination/start_minute/
    window_minutes/draft/height/margin/revision/blocked）。额外要求：

    * 泊位必须存在且属于终点（BERTH_NOT_FOUND / BERTH_NOT_AT_DESTINATION）；
    * 在最早抵达择优口径下，只接受"抵达即开工"可行的抵达分钟——作业
      [arrival, arrival+job) 完整落在同一开放区间、避开有效泊位占用；
      批次场景 extra_berth_occupancy 再并入本批前船拟占用（半开区间列表）；
      改期场景 exclude_trip_id 排除自身固化作业区间。
    * 窗口内航行可行但无满足泊位条件的抵达 -> NO_FEASIBLE_BERTH_ITINERARY。

    结果在 find_itinerary 输出上附加 berth_id/job_minutes/job_start_minute/
    job_end_minute（job_start_minute == arrival_minute，抵达即开工）。
    """
    destination = kwargs["destination"]
    start = kwargs["start_minute"]
    window = kwargs["window_minutes"]

    # 终点未知仍由 find_itinerary 统一报 NODE_NOT_FOUND；泊位存在性先于归属校验
    resolve_berth(conn, destination, berth_id)

    calendar = load_calendar(conn, berth_id)
    occupied = load_berth_occupancy(conn, berth_id,
                                    exclude_trip_id=exclude_trip_id)
    feasible = feasible_job_starts(
        calendar, occupied, job_minutes,
        lo=start, hi=start + window,
        extra_forbidden=extra_berth_occupancy,
    )

    try:
        result = find_itinerary(conn, arrival_ok=feasible, **kwargs)
    except ApiError as exc:
        if exc.code == "NO_FEASIBLE_ITINERARY":
            # 窗口内没有"抵达即开工"可行的行程：明确告知泊位维度无解
            raise no_berth_itinerary(berth_id, job_minutes)
        raise

    arrival = result["arrival_minute"]
    result["berth_id"] = berth_id
    result["job_minutes"] = job_minutes
    result["job_start_minute"] = arrival
    result["job_end_minute"] = arrival + job_minutes
    return result
