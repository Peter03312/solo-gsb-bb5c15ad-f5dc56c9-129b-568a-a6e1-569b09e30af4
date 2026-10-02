"""SQLAlchemy ORM 模型：货物、冷链事件（测温 / 交接）、车次、装卸时段与车载读数。"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventType(str, enum.Enum):
    """事件类型。成员名与 PG enum 字面量保持一致。"""

    measurement = "measurement"  # 测温
    handover = "handover"        # 交接


class StanceKind(str, enum.Enum):
    """责任方表态类型。"""

    confirm = "confirm"  # 确认担责
    dispute = "dispute"  # 异议（须附理由）


class IntervalSource(str, enum.Enum):
    """越温区间开启读数的来源。"""

    own = "own"            # 货物自有测温事件
    vehicle = "vehicle"    # 车载测温读数


class CorrectionTarget(str, enum.Enum):
    """测温更正的目标类型。"""

    own_event = "own_event"          # 货物自有测温事件
    vehicle_reading = "vehicle_reading"  # 车载测温读数


class Cargo(Base):
    __tablename__ = "cargo"

    cargo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temp_upper_limit: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    initial_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(Base):
    """一条货物自有测温或交接事件。

    测温：temperature 有值，giver/receiver 为空。
    交接：giver/receiver 有值，temperature 为空。
    交接是改变保管方的唯一事件；车载读数不改变保管方。
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "(event_type = 'measurement' AND temperature IS NOT NULL "
            "AND giver IS NULL AND receiver IS NULL) "
            "OR (event_type = 'handover' AND temperature IS NULL "
            "AND giver IS NOT NULL AND receiver IS NOT NULL AND giver <> receiver)",
            name="chk_event_shape",
        ),
        # 同一货物同一时刻只能有一笔测温（跨类型不互斥：测温与交接可同刻发生）
        Index(
            "ux_event_cargo_time_measurement",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'measurement'::event_type"),
            sqlite_where=text("event_type = 'measurement'"),
        ),
        # 同一货物同一时刻只能有一笔交接
        Index(
            "ux_event_cargo_time_handover",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'handover'::event_type"),
            sqlite_where=text("event_type = 'handover'"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", create_constraint=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    giver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Trip(Base):
    """一个冷链车次：可承载多票货物（经装卸时段登记挂接）。

    ``revision`` 为车次修订号（单调递增，初始 0）：该车次的装卸登记、
    车载读数写入、车载读数更正在同一事务内使其 +1。装卸前预检凭
    “预期车次修订号”做乐观并发校验，预检摘要亦绑定该修订号。
    """

    __tablename__ = "trip"

    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vehicle_no: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    revision: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Loading(Base):
    """一票货物在一个车次上的装载记录：装卸时段 ``[loaded_at, unloaded_at)``。

    左闭右开：读数时刻等于 loaded_at 时在车上，等于 unloaded_at 时已不在车上。
    同一货物的装卸时段不得跨车次重叠（应用侧在货物行锁内校验，保证并发一致）。
    主键为 (trip_id, cargo_id, loaded_at)：同车次同货物可有多段（分段承运）。
    """

    __tablename__ = "loading"
    __table_args__ = (
        CheckConstraint("loaded_at < unloaded_at", name="chk_loading_window"),
        Index("ix_loading_cargo", "cargo_id", "loaded_at", "unloaded_at"),
    )

    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    unloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class VehicleReading(Base):
    """车次上的一笔车载测温原始读数（测温记录）。

    读数只对读数时刻仍在车上的货物按其各自温度上限判断越温；是否真正被某货物
    采用（同刻该货物有自有测温时被取代）在合并读数时间线时计算，不落库。
    读数允许晚到（乱序写入），按 (trip_id, read_at) 唯一约束。
    """

    __tablename__ = "vehicle_reading"
    __table_args__ = (
        Index(
            "ux_vehicle_reading_trip_time",
            "trip_id",
            "read_at",
            unique=True,
        ),
        Index("ix_vehicle_reading_trip", "trip_id", "read_at"),
    )

    reading_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Stance(Base):
    """责任方对一个已闭合越温区间的一次表态（确认 / 异议）。

    区间可由货物自有测温或车载读数开启：``opener_source`` + 外键坐标二选一
    （start_event_id / vehicle_reading_id），对外的稳定区间标识为
    ``interval_key``（自有=事件ID；车载=``veh:<reading_id>``，见 app/chain.py）。

    表态是只追加（append-only）的：改口产生新编号的新记录，历次表态永久保留；
    晚到读数 / 事件重建后若区间边界或责任片段变化，旧行不删除、不修改，
    其是否仍“有效”由查询时与当前快照比对得出（见 app/responsibility.py）。
    """

    __tablename__ = "responsibility_stance"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'confirm' AND reason IS NULL) "
            "OR (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)",
            name="chk_stance_shape",
        ),
        CheckConstraint(
            "(opener_source = 'own' AND start_event_id IS NOT NULL "
            "AND vehicle_reading_id IS NULL) "
            "OR (opener_source = 'vehicle' AND vehicle_reading_id IS NOT NULL "
            "AND start_event_id IS NULL)",
            name="chk_stance_opener",
        ),
        Index("ix_stance_cargo_interval", "cargo_id", "interval_key"),
    )

    stance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 稳定区间标识：自有开启=测温事件ID；车载开启=veh:<reading_id>
    interval_key: Mapped[str] = mapped_column(String(68), nullable=False)
    opener_source: Mapped[IntervalSource] = mapped_column(
        Enum(IntervalSource, name="interval_source", create_constraint=True),
        nullable=False,
    )
    # 二选一的开启坐标（不再对外键做物理约束：车载区间对应读数表，且晚到重建后
    # 开启坐标可能变化；引用完整性由应用事务保证）
    start_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_reading_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responsible_party: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[StanceKind] = mapped_column(
        Enum(StanceKind, name="stance_kind", create_constraint=True),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 提交时校验通过的快照标识；区间/片段变化后该标识与当前快照不再一致
    snapshot_id: Mapped[str] = mapped_column(String(71), nullable=False)
    # 表态时该责任方承担的片段坐标（规范化 JSON：[[start,end,party],...]）。
    # 晚到事件重建后据此逐片段判定表态是否仍有效（未变化片段继续有效）。
    attested_segments: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TemperatureCorrection(Base):
    """测温值更正（append-only）：只改温度，时间 / 归属 / 原编号均不变。

    目标为一笔货物**自有测温事件**（``target_type='own_event'``，
    target_event_id 有值）或一笔**车载读数**（``target_type='vehicle_reading'``，
    target_reading_id 有值，记录所属车次）。交接事件不可更正，由应用侧拒绝。

    ``expected_version`` 为乐观锁：必须等于提交时目标的当前更正版本
    （首次更正为 0，随后依次递增）；``version`` 为本次更正后的版本
    （= expected_version + 1，也等于该目标下更正的写入序号）。
    原始测温 / 读数永不修改，当前有效温度取最新一条更正的 new_temperature；
    无更正时有效温度即原始温度、版本为 0。
    """

    __tablename__ = "temperature_correction"
    __table_args__ = (
        CheckConstraint(
            "new_temperature IS NOT NULL AND expected_version >= 0 "
            "AND version >= 1",
            name="chk_correction_shape",
        ),
        CheckConstraint(
            "(target_type = 'own_event' AND target_event_id IS NOT NULL "
            "AND target_reading_id IS NULL AND trip_id IS NULL) "
            "OR (target_type = 'vehicle_reading' "
            "AND target_reading_id IS NOT NULL AND trip_id IS NOT NULL "
            "AND target_event_id IS NULL AND cargo_id IS NULL)",
            name="chk_correction_target",
        ),
        # 每个目标下版本号唯一（应用侧在持锁期间按序号分配）
        Index(
            "ux_correction_event_version",
            "target_event_id",
            "version",
            unique=True,
        ),
        Index(
            "ux_correction_reading_version",
            "target_reading_id",
            "version",
            unique=True,
        ),
        Index(
            "ix_correction_event",
            "target_event_id",
            "created_at",
            "correction_id",
        ),
        Index(
            "ix_correction_reading",
            "target_reading_id",
            "created_at",
            "correction_id",
        ),
    )

    correction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_type: Mapped[CorrectionTarget] = mapped_column(
        Enum(CorrectionTarget, name="correction_target", create_constraint=True),
        nullable=False,
    )
    # own_event 目标坐标（target_type='own_event' 时有值）
    cargo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # vehicle_reading 目标坐标（target_type='vehicle_reading' 时有值）
    target_reading_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trip_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    expected_version: Mapped[int] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    new_temperature: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
