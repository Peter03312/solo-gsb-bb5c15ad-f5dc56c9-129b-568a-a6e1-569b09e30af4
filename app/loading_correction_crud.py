"""装卸时段更正的写入 / 查询事务。

装卸时段录错后，调度员可凭**更正编号**、**原车次 + 货物 + 首次装车时刻**
（窗口稳定身份）、**期望版本**、**修正后的带时区装卸时刻**与**原因**更正
已有登记。更正**只改该条装卸窗口**——车次、货物归属与窗口身份均不变；
当前 loading 行经“删旧行 + 插新行”替换（新行 ``registered_loaded_at``
保持首登装车时刻），原始窗口与历次更正只追加保留在 ``loading_correction``
表中永久可查。车次详情、越温报告、责任确认与同时越温查询均直接读取当前
loading 行，故一律反映更正后的当前窗口。

校验（全部失败均整次拒绝、不留部分变更）
----------------------------------------
* 新时段仍为左闭右开且起点严格早于终点，否则 ``422 invalid_loading_window``；
* 原因必须非空，否则 ``422 bad_loading_correction_shape``；
* 原窗口不存在（车次 / 货物 / 窗口）：分别 ``404 trip_not_found`` /
  ``404 cargo_not_found`` / ``404 loading_window_not_found``；
* ``expected_version`` 与窗口当前更正版本不一致 -> ``409 stale_version``；
* 重叠检查按“**同一货物在任意车次的时段不得重叠**”执行，且**排除该条旧
  窗口自身**（自身替换不算冲突，首尾相接允许）；冲突 ->
  ``409 loading_overlap``。

加锁与串行（全系统统一的锁顺序，见 app/vehicle_crud.py）
---------------------------------------------------------
**先按 cargo_id 升序锁定货物行，再锁定车次行**——与装卸登记、车载读数
写入 / 测温更正、该货物自有事件写入及责任表态严格串行；因此：

* 重叠校验所见即持锁期间该货物的全部最新窗口，窗口不会被并发改动；
* 车载读数是否适用、越温区间与责任片段的重建结果在持锁期间确定。

成功落库时同事务将车次修订号 +1（旧装卸预检摘要随之失效）。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crud import ApiError
from app.models import Loading, LoadingCorrection, Trip
from app.schemas import LoadingCorrectionCreate
from app.timeutil import ensure_utc
from app.vehicle_crud import (
    _load_trip_locked,
    _lock_cargo_rows,
    _windows_overlap,
)


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 读取辅助


def registered_loaded_at(loading: Loading):
    """窗口身份列：历史数据卷可能为 NULL，按首登装车时刻（loaded_at）兜底。"""
    reg = getattr(loading, "registered_loaded_at", None)
    return ensure_utc(reg) if reg is not None else ensure_utc(loading.loaded_at)


def load_corrections_for_window(
    db, trip_id: str, cargo_id: str, registered_loaded_at_
) -> list[LoadingCorrection]:
    """一条装卸窗口的全部更正（按版本升序）。"""
    return list(
        db.scalars(
            select(LoadingCorrection)
            .where(
                LoadingCorrection.trip_id == trip_id,
                LoadingCorrection.cargo_id == cargo_id,
                LoadingCorrection.registered_loaded_at == registered_loaded_at_,
            )
            .order_by(
                LoadingCorrection.version, LoadingCorrection.correction_id
            )
        ).all()
    )


def load_window_version_map(db, loadings: list[Loading]) -> dict[tuple, int]:
    """批量读取窗口当前更正版本：身份 (trip_id, cargo_id, registered_loaded_at)
    -> 最新版本（无更正为 0）。"""
    if not loadings:
        return {}
    identities = {
        (l.trip_id, l.cargo_id, registered_loaded_at(l)) for l in loadings
    }
    trip_ids = {t for t, _, _ in identities}
    cargo_ids = {c for _, c, _ in identities}
    rows = list(
        db.scalars(
            select(LoadingCorrection).where(
                LoadingCorrection.trip_id.in_(sorted(trip_ids)),
                LoadingCorrection.cargo_id.in_(sorted(cargo_ids)),
            )
        ).all()
    )
    latest: dict[tuple, int] = {}
    for row in rows:
        key = (
            row.trip_id,
            row.cargo_id,
            ensure_utc(row.registered_loaded_at),
        )
        if key in identities and row.version > latest.get(key, 0):
            latest[key] = row.version
    return latest


def _find_window(
    db, trip_id: str, cargo_id: str, registered_loaded_at_
) -> Loading | None:
    """按窗口稳定身份定位当前 loading 行（更正后主键已变，身份列不变）。"""
    candidates = list(
        db.scalars(
            select(Loading).where(
                Loading.trip_id == trip_id,
                Loading.cargo_id == cargo_id,
            )
        ).all()
    )
    for l in candidates:
        if registered_loaded_at(l) == registered_loaded_at_:
            l.loaded_at = ensure_utc(l.loaded_at)
            l.unloaded_at = ensure_utc(l.unloaded_at)
            return l
    return None


# ----------------------------------------------------------- 提交


@dataclass(frozen=True)
class WindowView:
    """对外展示的一条装卸窗口（当前时刻 + 窗口身份 + 当前更正版本）。"""

    trip_id: str
    cargo_id: str
    loaded_at: object
    unloaded_at: object
    registered_loaded_at: object
    version: int


@dataclass
class LoadingCorrectionResult:
    correction: LoadingCorrection
    replayed: bool
    window: WindowView


def _same_content(
    existing: LoadingCorrection,
    payload: LoadingCorrectionCreate,
    trip_id: str,
    registered_loaded_at_,
    new_loaded_at,
    new_unloaded_at,
) -> bool:
    """更正ID相同前提下逐字段比对内容（含窗口身份、期望版本、新时段与原因）。"""
    return (
        existing.trip_id == trip_id
        and existing.cargo_id == payload.cargo_id
        and ensure_utc(existing.registered_loaded_at) == registered_loaded_at_
        and existing.expected_version == payload.expected_version
        and ensure_utc(existing.new_loaded_at) == new_loaded_at
        and ensure_utc(existing.new_unloaded_at) == new_unloaded_at
        and (existing.reason or None) == payload.reason
    )


def _window_view_from_row(row: LoadingCorrection) -> WindowView:
    """幂等重传时由更正行还原“该次更正后的窗口”（原结果视图）。"""
    return WindowView(
        trip_id=row.trip_id,
        cargo_id=row.cargo_id,
        loaded_at=ensure_utc(row.new_loaded_at),
        unloaded_at=ensure_utc(row.new_unloaded_at),
        registered_loaded_at=ensure_utc(row.registered_loaded_at),
        version=row.version,
    )


def submit_loading_correction(
    db, trip_id: str, payload: LoadingCorrectionCreate
) -> LoadingCorrectionResult:
    reason = (payload.reason or "").strip()
    if not reason:
        raise _unprocessable(
            "装卸时段更正必须提供非空原因 reason",
            code="bad_loading_correction_shape",
        )
    payload.reason = reason

    new_loaded_at = ensure_utc(payload.new_loaded_at)
    new_unloaded_at = ensure_utc(payload.new_unloaded_at)
    if new_loaded_at >= new_unloaded_at:
        raise _unprocessable(
            f"车次 {trip_id} 货物 {payload.cargo_id} 的更正后装卸时段 "
            f"[{new_loaded_at.isoformat()}, {new_unloaded_at.isoformat()}) 无效："
            "装载时段必须左闭右开且起点严格早于终点",
            code="invalid_loading_window",
        )
    registered = ensure_utc(payload.loaded_at)

    # 幂等快路径：更正ID已存在时无需加锁即可裁决（同测温更正）
    existing = db.get(LoadingCorrection, payload.correction_id)
    if existing is not None:
        if not _same_content(
            existing, payload, trip_id, registered, new_loaded_at, new_unloaded_at
        ):
            raise _conflict(
                f"更正编号 {payload.correction_id} 已存在且内容不同，拒绝覆盖",
                code="loading_correction_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return LoadingCorrectionResult(
            correction=existing,
            replayed=True,
            window=_window_view_from_row(existing),
        )

    try:
        return _submit_tx(
            db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
        )
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _submit_tx(
            db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
        )


def _replay_if_winner(
    db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
) -> LoadingCorrectionResult | None:
    """并发竞争后按已落库的“赢家”行裁决：同内容返回原结果，异内容抛冲突。"""
    winner = db.get(LoadingCorrection, payload.correction_id)
    if winner is None:
        return None
    if not _same_content(
        winner, payload, trip_id, registered, new_loaded_at, new_unloaded_at
    ):
        raise _conflict(
            f"更正编号 {payload.correction_id} 已存在且内容不同，拒绝覆盖",
            code="loading_correction_id_conflict",
        )
    return LoadingCorrectionResult(
        correction=winner,
        replayed=True,
        window=_window_view_from_row(winner),
    )


def _submit_tx(
    db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
) -> LoadingCorrectionResult:
    try:
        # 车次须存在（先读；真正互斥在拿到车次行锁后）
        if db.get(Trip, trip_id) is None:
            raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

        # 1) 先锁货物行（与该货物事件 / 表态 / 读数 / 装卸登记串行）
        _lock_cargo_rows(db, [payload.cargo_id])
        # 2) 再锁车次行（与装卸登记 / 读数写入 / 测温更正串行）
        trip = _load_trip_locked(db, trip_id)

        # 持锁后判定幂等（并发重传在此串行分出唯一赢家）
        winner = db.get(LoadingCorrection, payload.correction_id)
        if winner is not None:
            if not _same_content(
                winner, payload, trip_id, registered,
                new_loaded_at, new_unloaded_at,
            ):
                raise _conflict(
                    f"更正编号 {payload.correction_id} 已存在且内容不同，拒绝覆盖",
                    code="loading_correction_id_conflict",
                )
            replay_view = _window_view_from_row(winner)
            db.rollback()  # 未做修改，仅结束事务释放行锁
            return LoadingCorrectionResult(
                correction=winner, replayed=True, window=replay_view
            )

        # 3) 原窗口须存在（车次 + 货物 + 首次装车时刻）
        old = _find_window(db, trip_id, payload.cargo_id, registered)
        if old is None:
            raise ApiError(
                404,
                "loading_window_not_found",
                f"车次 {trip_id} 货物 {payload.cargo_id} 不存在首次装车时刻为 "
                f"{registered.isoformat()} 的装卸窗口",
            )
        old_loaded_at, old_unloaded_at = old.loaded_at, old.unloaded_at

        # 4) 乐观锁：期望版本须等于窗口当前更正版本
        current_rows = load_corrections_for_window(
            db, trip_id, payload.cargo_id, registered
        )
        current_version = current_rows[-1].version if current_rows else 0
        if payload.expected_version != current_version:
            raise _conflict(
                f"版本过期：该装卸窗口当前更正版本为 {current_version}，"
                f"请求期望版本为 {payload.expected_version}，"
                "请重新查询车次详情（窗口当前版本）后再更正",
                code="stale_version",
            )

        # 5) 重叠检查：同一货物在任意车次的既有时段不得重叠，
        #    但排除该条旧窗口自身（自身替换）；首尾相接允许。
        existing = list(
            db.scalars(
                select(Loading).where(Loading.cargo_id == payload.cargo_id)
            ).all()
        )
        for l in existing:
            l.loaded_at = ensure_utc(l.loaded_at)
            l.unloaded_at = ensure_utc(l.unloaded_at)
        for l in existing:
            if l is old:
                continue
            # 身份相同的其他行理论上不存在（身份唯一），双保险一并排除
            if (
                l.trip_id == trip_id
                and registered_loaded_at(l) == registered
            ):
                continue
            if _windows_overlap(
                new_loaded_at, new_unloaded_at, l.loaded_at, l.unloaded_at
            ):
                raise _conflict(
                    f"货物 {payload.cargo_id} 更正后的装卸时段 "
                    f"[{new_loaded_at.isoformat()}, "
                    f"{new_unloaded_at.isoformat()}) 与车次 {l.trip_id} "
                    f"既有时段 [{l.loaded_at.isoformat()}, "
                    f"{l.unloaded_at.isoformat()}) 重叠；"
                    "同一货物不得同时属于两个车次",
                    code="loading_overlap",
                )

        # 6) 删旧行 + 插新行（窗口身份 registered_loaded_at 保持不变）
        db.delete(old)
        db.flush()
        new_row = Loading(
            trip_id=trip_id,
            cargo_id=payload.cargo_id,
            loaded_at=new_loaded_at,
            unloaded_at=new_unloaded_at,
            registered_loaded_at=registered,
        )
        db.add(new_row)

        # 7) 追加更正记录（append-only）
        version = current_version + 1
        correction = LoadingCorrection(
            correction_id=payload.correction_id,
            trip_id=trip_id,
            cargo_id=payload.cargo_id,
            registered_loaded_at=registered,
            old_loaded_at=old_loaded_at,
            old_unloaded_at=old_unloaded_at,
            new_loaded_at=new_loaded_at,
            new_unloaded_at=new_unloaded_at,
            expected_version=current_version,
            version=version,
            reason=payload.reason,
        )
        db.add(correction)
        # 装卸时段更正落库 -> 车次修订号 +1（失败回滚则不递增，旧预检摘要失效）
        trip.revision = (trip.revision or 0) + 1
        db.commit()
    except ApiError:
        db.rollback()
        # SQLite 文件库 FOR UPDATE 为空操作：版本过期可能只因同内容并发
        # 更正已先提交——此时按幂等返回原结果。
        replay = _replay_if_winner(
            db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _replay_if_winner(
            db, trip_id, payload, registered, new_loaded_at, new_unloaded_at
        )
        if replay is not None:
            return replay
        raise _conflict(
            f"装卸时段更正 {payload.correction_id} 与既有数据冲突"
            "（窗口版本号竞争 / 字段约束）",
            code="integrity_conflict",
        )

    correction.created_at = ensure_utc(correction.created_at)
    return LoadingCorrectionResult(
        correction=correction,
        replayed=False,
        window=WindowView(
            trip_id=trip_id,
            cargo_id=payload.cargo_id,
            loaded_at=new_loaded_at,
            unloaded_at=new_unloaded_at,
            registered_loaded_at=registered,
            version=version,
        ),
    )
