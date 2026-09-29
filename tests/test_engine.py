# -*- coding: utf-8 -*-
"""末日地堡生存 —— 核心引擎的可测试纯逻辑，验证资源守恒、危机决策、结局判定。

注意：测试使用独立内存级 Session，需清空表。为隔离，这里用 engine 建临时表。
"""
import pytest
from sqlalchemy.orm import Session

from app.core.database import Base, engine, SessionLocal
from app.core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from app.models import GameSession, Resident, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    CRISIS_POOL,
    FACILITY_ZH,
    FOOD,
    OXY,
    POWER,
    WATER,
)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def make_session(db, residents=3, resources=None):
    gs = GameSession(
        name="测试",
        day=1,
        target_day=SURVIVAL_TARGET_DAY,
        status="running",
        resources=resources or dict(INITIAL_RESOURCES),
        survivors=residents,
        score=0,
    )
    db.add(gs)
    db.flush()
    for i in range(residents):
        db.add(Resident(session_id=gs.id, name=f"人{i}", job="general", health=90, morale=80, alive=1, joined_day=1))
    for cat in ("power", "farm", "water", "oxygen"):
        db.add(Facility(session_id=gs.id, name=FACILITY_ZH[cat], category=cat, level=1, status="active", built_day=1))
    db.commit()
    db.refresh(gs)
    return gs


class FixedRand:
    """固定值随机 —— 每个 .random() 返回 0.9（不触发危机，因 0.9 > 0.45）。"""

    def random(self):
        return 0.9

    def choice(self, seq):
        return seq[0]


class ForceCrisisRand:
    """强制触发危机 —— random() 返回 0（必触发），目标选择 seq[0]。"""

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


def open_crisis(eng, key="sick"):
    """辅助：直接为档案挂起一个指定危机，返回待处理危机结构。"""
    event = next(e for e in CRISIS_POOL if e["key"] == key)
    return eng._open_crisis(event)


def test_advance_increments_day(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    assert gs.day == 2


def test_resources_change_with_population(db):
    """资源应有产出-消耗的净变化（守恒循环运行）。"""
    gs = make_session(db, residents=3)
    before = dict(gs.resources)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.advance_day()
    after = gs.resources
    # 至少一个资源发生变化
    assert any(abs(after[k] - before[k]) > 0.01 for k in ("food", "water", "power", "oxygen"))


def test_build_deducts_cost(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    food_before = gs.resources[FOOD]
    eng.build_facility("med")
    assert gs.resources[FOOD] < food_before
    assert any(f.category == "med" for f in gs.facilities)


def test_build_fails_when_poor(db):
    gs = make_session(db)
    gs.resources = {FOOD: 1, WATER: 1, POWER: 1, OXY: 1}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.build_facility("farm")


def test_upgrade_increases_level(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    fac = [f for f in gs.facilities if f.category == "farm"][0]
    eng.upgrade_facility(fac.id)
    assert fac.level == 2


def test_crisis_applies_resource_effects(db):
    """挂起的危机被结算后应扣除对应资源，并从存档清除。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    event = CRISIS_POOL[0]  # radstorm
    open_crisis(eng, event["key"])
    assert gs.pending_crisis is not None  # 待处理危机已纳入存档
    power_before = gs.resources[POWER]
    choice = event["choices"][0]  # shield_repair：电力 -8
    eng.resolve_crisis(event["key"], choice["key"])
    assert gs.resources[POWER] == round(power_before - 8, 1)
    assert gs.pending_crisis is None  # 结算后危机清除


def test_job_assignment_changes_resident(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    r = gs.residents[0]
    eng.set_job(r.id, "farmer")
    assert r.job == "farmer"


def test_win_at_target_day(db):
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY  # 目标天数
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "win"


def test_population_zero_ends_game(db):
    gs = make_session(db)
    for r in gs.residents:
        r.alive = 0
    gs.survivors = 0
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._check_end()
    assert gs.status == "over"


def test_advance_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.advance_day()


def test_morale_recovery_toward_75(db):
    gs = make_session(db)
    for r in gs.residents:
        r.morale = 40
    gs.resources = {FOOD: 999, WATER: 999, POWER: 999, OXY: 999}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._apply_health_morale()
    assert all(r.morale > 40 for r in gs.residents)


# ---- 目标绑定：危机触发时锁定目标，客户端无法换人/跨档案 ----

def test_bound_target_is_used_and_client_target_ignored(db):
    """单体效果作用于触发时绑定的目标；即便回传另一个居民编号也无效。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "sick")  # choice(seq)[0] 锁定目标为 0 号
    bound = next(r for r in gs.residents if r.id == crisis["target_id"])
    someone_else = next(r for r in gs.residents if r.id != bound.id)

    # 客户端尝试把目标伪造成另一名本档案居民
    eng.resolve_crisis("sick", "quarantine", target_id=someone_else.id)
    assert bound.health == 85  # 90 - 5，只伤绑定目标
    assert someone_else.health == 90


def test_client_cannot_swap_in_foreign_archive_target(db):
    """回传其他档案居民编号：不会作用于外人，效果仍落在本档案绑定目标。"""
    gs = make_session(db)
    other = make_session(db)
    foreign = other.residents[0]
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "sick")
    bound = next(r for r in gs.residents if r.id == crisis["target_id"])
    health_before_foreign = foreign.health

    eng.resolve_crisis("sick", "quarantine", target_id=foreign.id)
    assert foreign.health == health_before_foreign  # 外档案居民不受影响
    assert bound.health == 85


def test_dead_bound_target_rejected_and_no_partial_settle(db):
    """绑定目标在决策前死亡：单体结算拒绝，资源也不得扣减，危机仍挂起。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "sick")
    bound = next(r for r in gs.residents if r.id == crisis["target_id"])
    bound.alive = 0
    food_before = gs.resources[FOOD]
    health_before = [r.health for r in gs.residents]

    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=bound.id)
    # 未做任何部分结算，危机仍在档案中（玩家可改选全体决策）
    assert gs.resources[FOOD] == food_before
    assert [r.health for r in gs.residents] == health_before
    assert gs.pending_crisis is not None
    # 全体决策（public_health）不依赖目标，仍可正常结算
    eng.resolve_crisis("sick", "public_health", target_id=bound.id)
    assert gs.pending_crisis is None


def test_valid_target_only_affects_that_resident(db):
    """绑定目标：单体健康效果只作用于其本人，不波及其他居民。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "raid")  # defend：健康 -8
    target = next(r for r in gs.residents if r.id == crisis["target_id"])
    others = [r for r in gs.residents if r.id != target.id]
    others_before = [r.health for r in others]
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    assert target.health == 82  # 90 - 8
    assert [r.health for r in others] == others_before


def test_no_target_applies_to_all_alive(db):
    """全体士气事件：无绑定目标，效果作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 士气 +20
    assert all(r.morale == 100 for r in gs.residents if r.alive)


# ---- 前后端目标语义统一：作用域由事件效果声明 ----

def test_all_scope_crisis_carries_no_target(db):
    """内讧（纯全体士气事件）挂起时不应随机出目标。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = open_crisis(eng, "mutiny")
    assert crisis["needs_target"] is False
    assert crisis["target_id"] is None
    assert crisis["target_name"] is None
    assert all(c["targeted"] is False for c in crisis["choices"])


def test_single_scope_crisis_carries_target_and_flags(db):
    """疫病存在单体决策，须随机目标；隔离=单人，全员消毒=全体。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    crisis = open_crisis(eng, "sick")
    assert crisis["needs_target"] is True
    assert crisis["target_id"] is not None
    flags = {c["key"]: c["targeted"] for c in crisis["choices"]}
    assert flags == {"quarantine": True, "public_health": False}


def test_global_morale_ignores_client_target(db):
    """全体士气决策即便带了目标编号，仍作用于全体存活者。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    random_target = gs.residents[0]
    others = [r for r in gs.residents if r.id != random_target.id]
    before = {r.id: r.morale for r in gs.residents}
    eng.resolve_crisis("mutiny", "suppress", target_id=random_target.id)  # 士气 -15
    assert random_target.morale == before[random_target.id] - 15
    for r in others:
        assert r.morale == before[r.id] - 15


def test_global_scope_ignores_even_foreign_target(db):
    """全体效果不做目标校验：跨档案编号也不会让结算失败或作用于单人。"""
    gs = make_session(db)
    other = make_session(db)
    foreign_id = other.residents[0].id
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "suppress", target_id=foreign_id)
    assert all(r.morale == 65 for r in gs.residents if r.alive)


def test_single_vs_all_choice_scope_within_one_event(db):
    """同一疫病事件：隔离只伤绑定目标，全员消毒不动任何人健康。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "sick")
    target = next(r for r in gs.residents if r.id == crisis["target_id"])

    # 全员消毒先结算（资源效果，无健康伤害）
    eng.resolve_crisis("sick", "public_health", target_id=target.id)
    assert all(r.health == 90 for r in gs.residents)

    # 再次挂起同一事件，隔离只伤绑定目标
    crisis = open_crisis(eng, "sick")
    target = next(r for r in gs.residents if r.id == crisis["target_id"])
    eng.resolve_crisis("sick", "quarantine", target_id=target.id)
    assert target.health == 85
    assert all(r.health == 90 for r in gs.residents if r.id != target.id)


def test_log_scope_matches_settlement(db):
    """日志作用域标注必须与实际结算一致：单体写姓名，全体写全体。"""
    from app.models import EventLog

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "raid")
    target = next(r for r in gs.residents if r.id == crisis["target_id"])
    eng.resolve_crisis("raid", "defend", target_id=target.id)
    open_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")
    db.commit()

    logs = db.query(EventLog).filter_by(session_id=gs.id).order_by(EventLog.id).all()
    single_log = next(l for l in logs if "武装抵抗" in (l.detail or ""))
    global_log = next(l for l in logs if "加倍发放食物" in (l.detail or ""))
    assert target.name in single_log.detail
    assert "全体" in global_log.detail


def test_resource_change_persists_across_sessions(db):
    """资源与待处理危机须真正落库（重新打开会话仍可见）。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    sid = gs.id
    before = gs.resources[FOOD]
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    eng.resolve_crisis("mutiny", "double_ration")  # 食物 -20
    db.commit()

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.resources[FOOD] == round(max(0.0, before - 20), 1)
        assert reloaded.pending_crisis is None
    finally:
        db2.close()


def test_pending_crisis_persists_and_restores(db):
    """挂起危机后提交、重开档案：决策内容与绑定目标原样恢复。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = open_crisis(eng, "sick")
    db.commit()
    sid = gs.id
    bound_id = crisis["target_id"]

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.pending_crisis is not None
        assert reloaded.pending_crisis["event"] == "sick"
        assert reloaded.pending_crisis["target_id"] == bound_id
        # 恢复后仍可正常结算
        eng2 = BunkerEngine(db2, reloaded)
        eng2.resolve_crisis("sick", "quarantine", target_id=bound_id)
        db2.commit()
        assert reloaded.pending_crisis is None
    finally:
        db2.close()


# ---- 结算边界：已结束档案拒绝一切状态变更 ----

def test_actions_rejected_after_game_end(db):
    gs = make_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=FixedRand())
    rid = gs.residents[0].id
    fid = gs.facilities[0].id
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("sick", "quarantine", target_id=rid)
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")


# ---- 危机不可跳过：待处理期间冻结一切推进/经营 ----

def test_crisis_blocks_advance(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    day_before = gs.day
    with pytest.raises(BunkerEngineError):
        eng.advance_day()
    assert gs.day == day_before  # 天数没有被推进


def test_crisis_blocks_build_upgrade_job(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    fid = gs.facilities[0].id
    rid = gs.residents[0].id
    food_before = gs.resources[FOOD]
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")
    with pytest.raises(BunkerEngineError):
        eng.upgrade_facility(fid)
    with pytest.raises(BunkerEngineError):
        eng.set_job(rid, "farmer")
    # 未产生任何副作用
    assert gs.resources[FOOD] == food_before
    assert gs.residents[0].job == "general"
    assert gs.pending_crisis is not None


def test_advance_persists_crisis_and_next_advance_blocked(db):
    """推进触发危机后提交；重开会话再次推进应被拒绝（刷新无法跳过）。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = eng.advance_day()  # 必触发危机
    assert crisis is not None and crisis["event"]
    db.commit()
    sid = gs.id
    day = gs.day

    db2 = SessionLocal()
    try:
        reloaded = db2.get(GS, sid)
        assert reloaded.pending_crisis is not None
        eng2 = BunkerEngine(db2, reloaded, rand=ForceCrisisRand())
        with pytest.raises(BunkerEngineError):
            eng2.advance_day()
        assert reloaded.day == day
    finally:
        db2.close()


# ---- 危机不可伪造：无待处理事件时无法凭空结算 ----

def test_resolve_without_pending_crisis_rejected(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    food_before = gs.resources[FOOD]
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "suppress")
    assert gs.resources[FOOD] == food_before
    morale = [r.morale for r in gs.residents]
    assert morale == [80, 80, 80]


def test_resolve_wrong_event_key_rejected(db):
    """存档挂的是 A 事件，提交 B 事件键：拒绝且不结算。"""
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    morale_before = [r.morale for r in gs.residents]
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("leak", "emergency_repair")
    assert gs.pending_crisis is not None
    assert gs.pending_crisis["event"] == "mutiny"
    assert [r.morale for r in gs.residents] == morale_before


def test_resolve_unknown_choice_rejected_and_keeps_pending(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "not_a_real_choice")
    assert gs.pending_crisis is not None


# ---- 危机不可重复结算：同一事件只生效一次 ----

def test_duplicate_resolve_settles_only_once(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")  # double_ration：食物 -20，士气 +20
    food_before = gs.resources[FOOD]

    eng.resolve_crisis("mutiny", "double_ration")
    assert gs.resources[FOOD] == round(food_before - 20, 1)
    # 重复提交同一事件/同一选项：必须拒绝，不能再扣一次食物
    with pytest.raises(BunkerEngineError):
        eng.resolve_crisis("mutiny", "double_ration")
    assert gs.resources[FOOD] == round(food_before - 20, 1)
    # 士气也只结算一次；验证只有一条危机结算日志
    db.flush()
    from app.models import EventLog
    logs = db.query(EventLog).filter_by(session_id=gs.id).count()
    assert logs == 1  # 只有一条危机结算日志


def test_concurrent_resolve_settles_only_once(db):
    """两个独立 DB 会话并发结算同一危机：只允许一个生效。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    db.commit()
    sid = gs.id

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        gs_a = db_a.get(GS, sid)
        gs_b = db_b.get(GS, sid)
        eng_a = BunkerEngine(db_a, gs_a)
        eng_b = BunkerEngine(db_b, gs_b)

        eng_a.resolve_crisis("mutiny", "double_ration")
        db_a.commit()

        # b 持有的是提交前的旧快照（pending_crisis 非空），其条件更新
        # 必须匹配不到行而失败
        with pytest.raises(BunkerEngineError):
            eng_b.resolve_crisis("mutiny", "suppress")
        db_b.rollback()
    finally:
        db_a.close()
        db_b.close()

    # 用全新会话读取（fixture 主会话持有测试开始前的旧事务快照）
    db_f = SessionLocal()
    try:
        final = db_f.get(GS, sid)
        assert final.pending_crisis is None
        # 只扣了一次食物（-20），不是 -35
        assert final.resources[FOOD] == round(dict(INITIAL_RESOURCES)[FOOD] - 20, 1)
    finally:
        db_f.close()


def test_concurrent_advance_advances_only_one_day(db):
    """两个独立会话并发推进同一天：只有一个成功，天数只 +1。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    sid = gs.id
    db.commit()

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        gs_a = db_a.get(GS, sid)
        gs_b = db_b.get(GS, sid)
        eng_a = BunkerEngine(db_a, gs_a, rand=FixedRand())
        eng_b = BunkerEngine(db_b, gs_b, rand=FixedRand())

        eng_a.advance_day()
        db_a.commit()

        with pytest.raises(BunkerEngineError):
            eng_b.advance_day()
        db_b.rollback()
    finally:
        db_a.close()
        db_b.close()

    db_f = SessionLocal()
    try:
        assert db_f.get(GS, sid).day == 2
    finally:
        db_f.close()


# ---- 终局流转：每日推进 / 危机结算 / 结局统一收口 ----

def test_advance_to_target_day_ends_without_pending_crisis(db):
    """抵达目标日直接胜利，不再挂起危机（即使随机数强制触发）。"""
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    gs.day = SURVIVAL_TARGET_DAY - 1
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = eng.advance_day()
    assert crisis is None
    assert gs.status == "win"
    assert gs.pending_crisis is None
    assert gs.outcome and gs.outcome["win"] is True


def test_resolve_can_end_game_and_clears_pending(db):
    """危机全体士气/资源效果若导致终局，状态正确流转且危机清空。"""
    gs = make_session(db)
    # 资源压到全线枯竭边缘：任意净扣都会触发全线枯竭判定
    gs.resources = {FOOD: 0.5, WATER: 0.5, POWER: 0.5, OXY: 0.5}
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "leak")  # emergency_repair：食物 -6、电力 -6
    eng.resolve_crisis("leak", "emergency_repair")
    assert gs.status == "over"
    assert gs.pending_crisis is None
    assert gs.outcome and gs.outcome["win"] is False


def test_end_game_clears_any_pending_crisis(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    open_crisis(eng, "mutiny")
    assert gs.pending_crisis is not None
    for r in gs.residents:
        r.alive = 0
    gs.survivors = 0
    ended = eng._check_end()
    assert ended is True
    assert gs.status == "over"
    assert gs.pending_crisis is None


# ---- 旧档案兼容：缺列的旧数据库可自动迁移，旧档案行为等同无待处理危机 ----

def test_legacy_schema_migration_adds_column(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from app.core.database import Base, ensure_schema

    legacy_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    # 1) 先按当前模型建库
    first = create_engine(legacy_url)
    Base.metadata.create_all(bind=first)
    # 2) 删掉新列，模拟旧版本数据库
    with first.begin() as conn:
        conn.execute(text("CREATE TABLE gs_backup AS SELECT id, name, day, target_day, status, resources, survivors, outcome, score, created_at, updated_at FROM game_sessions"))
        conn.execute(text("DROP TABLE game_sessions"))
        conn.execute(text("ALTER TABLE gs_backup RENAME TO game_sessions"))
    first.dispose()

    # 3) ensure_schema 应幂等补列且不报错
    legacy_engine = create_engine(legacy_url)
    ensure_schema(bind=legacy_engine)
    cols = {c["name"] for c in inspect(legacy_engine).get_columns("game_sessions")}
    assert "pending_crisis" in cols
    # 再跑一次也安全
    ensure_schema(bind=legacy_engine)
    legacy_engine.dispose()


def test_legacy_archive_without_pending_plays_normally(db):
    """旧档案 pending_crisis 为 None：可正常推进、触发并结算危机。"""
    gs = make_session(db)
    assert gs.pending_crisis is None
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = eng.advance_day()
    assert crisis is not None
    assert gs.day == 2
    assert gs.pending_crisis is not None
    eng2 = BunkerEngine(db, gs)
    eng2.resolve_crisis(crisis["event"], crisis["choices"][0]["key"], target_id=crisis["target_id"])
    assert gs.pending_crisis is None


def test_advance_after_resolved_crisis_new_session(db):
    """回归：危机结算后 pending_crisis 落库为 JSON 'null'（非 SQL NULL），
    跨会话推进必须仍能识别为「无待处理危机」，不得永久卡死。"""
    from app.core.database import SessionLocal
    from app.models import GameSession as GS

    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=ForceCrisisRand())
    crisis = eng.advance_day()
    db.commit()
    sid = gs.id

    db2 = SessionLocal()
    try:
        g2 = db2.get(GS, sid)
        BunkerEngine(db2, g2).resolve_crisis(
            crisis["event"], crisis["choices"][0]["key"], target_id=crisis["target_id"]
        )
        db2.commit()
    finally:
        db2.close()

    # 数据库中实际存储形态确认是 JSON 文本 'null'
    import sqlalchemy as sa
    raw = db.execute(sa.text("SELECT pending_crisis FROM game_sessions WHERE id=:i"), {"i": sid}).fetchone()
    db.rollback()
    assert raw[0] in (None, "null")

    db3 = SessionLocal()
    try:
        g3 = db3.get(GS, sid)
        assert g3.pending_crisis is None
        BunkerEngine(db3, g3, rand=FixedRand()).advance_day()
        assert g3.day == 3
        db3.commit()
    finally:
        db3.close()