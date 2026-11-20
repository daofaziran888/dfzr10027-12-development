"""实验就绪与转化分析测试：按观测期校验样本、缺数组别、集市同步与试投排名。"""
from __future__ import annotations

import pytest

from app.routers.experiment_mart import summarize_periods
from app.services import experiment_readiness as readiness
from app.services.pipeline_orchestrator import run_experiment_stat_pipeline
from app.store import store


def _set_thresholds(value: int = 500) -> None:
    readiness.set_min_sample(1, value)  # EXPE-001
    readiness.set_min_sample(2, value)  # EXPE-002


def _ingest(code: str, period: str, group: str, n: int, conv: int, batch: str) -> dict:
    row, message = readiness.ingest_observation({
        "实验编号": code, "观测期": period, "分组": group,
        "样本量": n, "转化人数": conv, "批次号": batch,
    })
    assert row is not None, message
    return row


@pytest.fixture()
def scenario():
    """甲两组达标且提升为正；乙本期 1300 实验 / 300 对照，对照组不足。"""
    _set_thresholds(500)
    _ingest("EXPE-002", "2026-09-01", "treatment", 1300, 100, "b2-t")
    _ingest("EXPE-002", "2026-09-01", "control", 300, 20, "b2-c")
    return readiness.build_readiness("EXPE-002")


class TestPerGroupThreshold:
    def test_threshold_is_per_group_not_total(self):
        """500 门槛按组计：两组各 500 达标，而不是合计 500。"""
        _set_thresholds(500)
        _ingest("EXPE-002", "2026-09-01", "treatment", 500, 30, "t")
        _ingest("EXPE-002", "2026-09-01", "control", 499, 30, "c")
        result = readiness.build_readiness("EXPE-002")
        assert result["ready"] is False
        assert result["缺数组别"] == ["control"]
        assert result["groups"]["treatment"]["threshold_met"] is True
        assert result["groups"]["control"]["gap"] == 1

    def test_ready_requires_both_groups(self):
        _set_thresholds(500)
        _ingest("EXPE-002", "2026-09-01", "treatment", 500, 40, "t")
        _ingest("EXPE-002", "2026-09-01", "control", 500, 30, "c")
        result = readiness.build_readiness("EXPE-002")
        assert result["ready"] is True
        assert result["readiness_status"] == readiness.READY
        assert result["缺数组别"] == []

    def test_missing_group_listed_without_lift(self, scenario):
        assert scenario["ready"] is False
        assert scenario["缺数组别名称"] == ["对照组"]
        assert scenario["lift"] is None
        assert scenario["published_significance"] is None
        assert scenario["review_status"] == readiness.REVIEW_NOT_READY

    def test_min_sample_update_validation(self):
        entry, message = readiness.set_min_sample(1, 0)
        assert entry is None and "大于 0" in message
        entry, _ = readiness.set_min_sample(1, 500)
        assert entry["最小样本"] == "500"


class TestObservationPeriodIsolation:
    def test_late_next_period_control_does_not_upgrade_current(self, scenario):
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        result = readiness.build_readiness("EXPE-002")
        assert result["observation_period"] == "2026-09-01"
        assert result["ready"] is False
        assert result["groups"]["control"]["sample_size"] == 300
        pending = {item["observation_period"]: item for item in result["pending_periods"]}
        assert pending["2026-09-08"]["missing_group_codes"] == ["treatment"]

    def test_periods_never_merged(self, scenario):
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        result = readiness.build_readiness("EXPE-002")
        for period in result["periods"]:
            sample = sum(period["groups"][g]["sample_size"] for g in ("control", "treatment"))
            if period["observation_period"] == "2026-09-01":
                assert sample == 1600
            else:
                assert sample == 600

    def test_completing_current_period_keeps_its_own_period(self, scenario):
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        _ingest("EXPE-002", "2026-09-01", "control", 200, 15, "b2-c2")
        result = readiness.build_readiness("EXPE-002")
        assert result["observation_period"] == "2026-09-01"
        assert result["ready"] is True
        assert result["groups"]["control"]["sample_size"] == 500
        assert result["groups"]["treatment"]["sample_size"] == 1300

    def test_duplicate_batch_overwrites_not_accumulates(self):
        _set_thresholds(500)
        _ingest("EXPE-001", "2026-10-01", "control", 400, 20, "same")
        _ingest("EXPE-001", "2026-10-01", "control", 450, 25, "same")
        result = readiness.build_readiness("EXPE-001")
        assert result["groups"]["control"]["sample_size"] == 450
        assert result["groups"]["control"]["batch_count"] == 1


class TestReviewDistinguishesStates:
    def test_not_ready_review(self, scenario):
        review = readiness.build_review("EXPE-002")
        assert review["复盘状态"] == readiness.REVIEW_NOT_READY
        assert review["可发布显著性"] is False
        assert review["缺口明细"][0]["分组"] == "control"
        assert review["缺口明细"][0]["缺口"] == 200
        assert "样本未达" in review["说明"]

    def test_completed_but_not_significant_review(self, scenario):
        _ingest("EXPE-002", "2026-09-01", "control", 200, 15, "b2-c2")
        readiness.sync_experiment("EXPE-002")
        review = readiness.build_review("EXPE-002")
        assert review["复盘状态"] == readiness.REVIEW_NOT_SIGNIFICANT
        assert review["可发布显著性"] is True
        assert review["统计"]["显著"] is False
        assert review["缺口明细"] == []
        assert "不是样本未到齐" in review["说明"]

    def test_significant_ready_experiment(self):
        _set_thresholds(500)
        review = readiness.build_review("EXPE-001")
        assert review["复盘状态"] == readiness.REVIEW_SIGNIFICANT
        assert review["提升量"] > 0
        assert review["统计"]["显著"] is True


class TestMartSync:
    def test_not_ready_row_keeps_missing_groups_without_lift(self, scenario):
        readiness.sync_experiment("EXPE-002")
        rows = [r for r in store.rows("data_mart") if r.get("实验编号") == "EXPE-002"]
        assert len(rows) == 1
        row = rows[0]
        assert row["缺数组别"] == ["control"]
        assert row["提升量"] is None
        assert row["可参与排名"] is False
        assert row["就绪状态"] == readiness.NOT_READY

    def test_sync_is_idempotent_per_experiment_period(self, scenario):
        readiness.sync_experiment("EXPE-002")
        readiness.sync_experiment("EXPE-002")
        rows = [r for r in store.rows("data_mart") if r.get("实验编号") == "EXPE-002"]
        assert len(rows) == 1

    def test_sync_writes_only_current_period_row(self, scenario):
        """同步只写本期一行；其它观测期的批次不会混进这一行。"""
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        _ingest("EXPE-002", "2026-09-08", "treatment", 700, 50, "b3-t")
        readiness.sync_experiment("EXPE-002")
        rows = [r for r in store.rows("data_mart") if r.get("实验编号") == "EXPE-002"]
        assert len(rows) == 1
        # 09-08 是唯一两组齐全的观测期，本期落在 09-08；样本只来自 09-08
        assert rows[0]["观测期"] == "2026-09-08"
        assert rows[0]["对照组样本量"] == 600
        assert rows[0]["实验组样本量"] == 700

    def test_current_period_stays_when_next_period_incomplete(self, scenario):
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        readiness.sync_experiment("EXPE-002")
        rows = [r for r in store.rows("data_mart") if r.get("实验编号") == "EXPE-002"]
        assert [r["观测期"] for r in rows] == ["2026-09-01"]

    def test_mart_aggregate_excludes_unready(self, scenario):
        readiness.sync_experiment("EXPE-001")
        readiness.sync_experiment("EXPE-002")
        summary = summarize_periods()
        assert summary["ready"] == 1
        assert summary["not_ready"] == 1
        assert summary["aggregate"]["lift_rows"] == 1
        assert summary["aggregate"]["lift_mean"] == pytest.approx(0.0124, abs=1e-6)

    def test_pipeline_returns_readiness_and_publishes_only_when_ready(self, scenario):
        result = run_experiment_stat_pipeline("EXPE-002")
        assert result["ready"] is False
        assert result["published_significance"] is None
        assert result["published_lift"] is None
        assert result["缺数组别"] == ["control"]

        result = run_experiment_stat_pipeline("EXPE-001")
        assert result["ready"] is True
        assert result["published_significance"] is True
        assert result["published_lift"] == pytest.approx(0.0124, abs=1e-6)

    def test_pipeline_batch_covers_all_analysis_experiments(self, scenario):
        result = run_experiment_stat_pipeline()
        assert result["mode"] == "all"
        assert result["experiment_count"] == 2
        statuses = {item["experiment_code"]: item["ready"] for item in result["items"]}
        assert statuses == {"EXPE-001": True, "EXPE-002": False}


class TestRanking:
    def test_ranking_uses_mart_lift_and_lists_unranked(self, scenario):
        run_experiment_stat_pipeline()
        ranking = readiness.build_ranking()
        assert [item["实验编号"] for item in ranking["items"]] == ["EXPE-001"]
        assert ranking["items"][0]["排名"] == 1
        assert ranking["items"][0]["提升量"] == pytest.approx(0.0124, abs=1e-6)
        assert [item["实验编号"] for item in ranking["unranked"]] == ["EXPE-002"]
        assert ranking["unranked"][0]["原因"] == "样本未到齐"
        assert ranking["unranked"][0]["缺数组别"] == ["control"]
        assert ranking["coverage"] == {
            "total_experiments": 2, "ranked": 1, "unranked": 1, "coverage_rate": 0.5,
        }
        assert ranking["can_arrange_ready"] is True
        assert ranking["covers_all_experiments"] is False

    def test_late_next_period_data_does_not_change_this_period_ranking(self, scenario):
        run_experiment_stat_pipeline()
        before = readiness.build_ranking()
        _ingest("EXPE-002", "2026-09-08", "control", 600, 40, "b3-c")
        after = readiness.build_ranking()
        assert before["items"] == after["items"]
        assert before["unranked"] == after["unranked"]

    def test_ranking_includes_completed_not_significant_experiment(self, scenario):
        _ingest("EXPE-002", "2026-09-01", "control", 200, 15, "b2-c2")
        run_experiment_stat_pipeline()
        ranking = readiness.build_ranking()
        codes = [item["实验编号"] for item in ranking["items"]]
        assert codes == ["EXPE-001", "EXPE-002"]
        assert ranking["items"][1]["显著性"] is False
        assert ranking["unranked"] == []
        assert ranking["covers_all_experiments"] is True

    def test_unsynced_ready_experiment_not_ranked(self, scenario):
        # EXPE-001 就绪但从未同步，集市里没有行 -> 不能凭空排名
        ranking = readiness.build_ranking()
        assert ranking["items"] == []
        reasons = {item["实验编号"]: item["原因"] for item in ranking["unranked"]}
        assert reasons["EXPE-001"] == "本期统计尚未同步到集市"


class TestInputValidation:
    def test_reject_conversions_over_sample(self):
        row, message = readiness.ingest_observation({
            "实验编号": "EXPE-001", "观测期": "2026-09-01", "分组": "control",
            "样本量": 10, "转化人数": 11,
        })
        assert row is None and "不能大于" in message

    def test_reject_bad_period(self):
        row, message = readiness.ingest_observation({
            "实验编号": "EXPE-001", "观测期": "09/01", "分组": "control", "样本量": 10,
        })
        assert row is None and "YYYY-MM-DD" in message

    def test_accept_chinese_group_labels(self):
        row, _ = readiness.ingest_observation({
            "实验编号": "EXPE-001", "观测期": "2026-09-01", "分组": "对照组", "样本量": 10,
        })
        assert row["分组"] == "control"
