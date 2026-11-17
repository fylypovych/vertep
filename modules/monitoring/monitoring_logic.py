import os
import json
import logging
import pg8000.native
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@dataclass
class Metric:
    id: str
    name: str
    value: float
    labels: Dict[str, str]
    timestamp: str
    node_id: Optional[str]


@dataclass
class Alert:
    id: str
    name: str
    severity: str  # info, warning, critical
    condition: str
    message: str
    active: bool
    fired_at: Optional[str]
    resolved_at: Optional[str]
    labels: Dict[str, str]


@dataclass
class HealthCheck:
    id: str
    component: str
    status: str  # healthy, degraded, unhealthy
    message: str
    details: Dict[str, Any]
    checked_at: str


@dataclass
class SystemLog:
    id: int
    level: str  # debug, info, warning, error, critical
    component: str
    message: str
    context: Dict[str, Any]
    timestamp: str


class MonitoringModule:
    def __init__(self, db_url: str):
        self.db_url = db_url

    @contextmanager
    def _conn(self):
        conn = pg8000.native.Connection(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def _row_to_metric(self, row) -> Metric:
        return Metric(
            id=row[0],
            name=row[1],
            value=row[2],
            labels=json.loads(row[3]) if row[3] else {},
            timestamp=str(row[4]),
            node_id=row[5],
        )

    def _row_to_alert(self, row) -> Alert:
        return Alert(
            id=row[0],
            name=row[1],
            severity=row[2],
            condition=row[3],
            message=row[4],
            active=row[5],
            fired_at=row[6],
            resolved_at=row[7],
            labels=json.loads(row[8]) if row[8] else {},
        )

    def _row_to_health(self, row) -> HealthCheck:
        return HealthCheck(
            id=row[0],
            component=row[1],
            status=row[2],
            message=row[3],
            details=json.loads(row[4]) if row[4] else {},
            checked_at=str(row[5]),
        )

    def _row_to_log(self, row) -> SystemLog:
        return SystemLog(
            id=row[0],
            level=row[1],
            component=row[2],
            message=row[3],
            context=json.loads(row[4]) if row[4] else {},
            timestamp=str(row[5]),
        )

    # Metrics
    def record_metric(self, metric: Metric) -> Metric:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO metrics (id, name, value, labels, timestamp, node_id)
                VALUES (:id, :name, :value, :labels, :timestamp, :node_id)
                """,
                id=metric.id,
                name=metric.name,
                value=metric.value,
                labels=json.dumps(metric.labels),
                timestamp=metric.timestamp,
                node_id=metric.node_id,
            )
            return metric

    def get_metrics(self, name: Optional[str] = None, node_id: Optional[str] = None, since: Optional[str] = None, limit: int = 1000) -> List[Metric]:
        with self._conn() as conn:
            sql = "SELECT * FROM metrics"
            params = {}
            conditions = []
            if name:
                conditions.append("name = :name")
                params["name"] = name
            if node_id:
                conditions.append("node_id = :node_id")
                params["node_id"] = node_id
            if since:
                conditions.append("timestamp >= :since")
                params["since"] = since
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY timestamp DESC LIMIT :limit"
            params["limit"] = limit
            rows = conn.run(sql, **params)
            return [self._row_to_metric(r) for r in rows]

    def get_latest_metrics(self, node_id: Optional[str] = None) -> List[Metric]:
        with self._conn() as conn:
            sql = """
                SELECT DISTINCT ON (name, node_id) * FROM metrics
            """
            params = {}
            conditions = []
            if node_id:
                conditions.append("node_id = :node_id")
                params["node_id"] = node_id
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY name, node_id, timestamp DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_metric(r) for r in rows]

    # Alerts
    def create_alert(self, alert: Alert) -> Alert:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO alerts (id, name, severity, condition, message, active, fired_at, resolved_at, labels)
                VALUES (:id, :name, :severity, :condition, :message, :active, :fired_at, :resolved_at, :labels)
                """,
                id=alert.id,
                name=alert.name,
                severity=alert.severity,
                condition=alert.condition,
                message=alert.message,
                active=alert.active,
                fired_at=alert.fired_at,
                resolved_at=alert.resolved_at,
                labels=json.dumps(alert.labels),
            )
            return alert

    def get_alert(self, alert_id: str) -> Optional[Alert]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM alerts WHERE id = :id", id=alert_id)
            return self._row_to_alert(rows[0]) if rows else None

    def list_alerts(self, active_only: bool = False, severity: Optional[str] = None) -> List[Alert]:
        with self._conn() as conn:
            sql = "SELECT * FROM alerts"
            params = {}
            conditions = []
            if active_only:
                conditions.append("active = true")
            if severity:
                conditions.append("severity = :severity")
                params["severity"] = severity
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY fired_at DESC NULLS LAST"
            rows = conn.run(sql, **params)
            return [self._row_to_alert(r) for r in rows]

    def update_alert(self, alert_id: str, updates: Dict[str, Any]) -> Optional[Alert]:
        with self._conn() as conn:
            alert = self.get_alert(alert_id)
            if not alert:
                return None
            allowed = {"active", "message", "resolved_at", "severity"}
            set_clauses = []
            params = {"id": alert_id}
            for k, v in updates.items():
                if k in allowed:
                    set_clauses.append(f"{k} = :{k}")
                    params[k] = v
            if not set_clauses:
                return alert
            conn.run(f"UPDATE alerts SET {', '.join(set_clauses)} WHERE id = :id", **params)
            return self.get_alert(alert_id)

    def fire_alert(self, alert_id: str, message: str) -> Optional[Alert]:
        return self.update_alert(alert_id, {"active": True, "fired_at": datetime.utcnow().isoformat(), "message": message})

    def resolve_alert(self, alert_id: str) -> Optional[Alert]:
        return self.update_alert(alert_id, {"active": False, "resolved_at": datetime.utcnow().isoformat()})

    # Health Checks
    def record_health_check(self, health: HealthCheck) -> HealthCheck:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO health_checks (id, component, status, message, details, checked_at)
                VALUES (:id, :component, :status, :message, :details, :checked_at)
                """,
                id=health.id,
                component=health.component,
                status=health.status,
                message=health.message,
                details=json.dumps(health.details),
                checked_at=health.checked_at,
            )
            return health

    def get_latest_health(self, component: Optional[str] = None) -> List[HealthCheck]:
        with self._conn() as conn:
            sql = """
                SELECT DISTINCT ON (component) * FROM health_checks
            """
            params = {}
            if component:
                sql += " WHERE component = :component"
                params["component"] = component
            sql += " ORDER BY component, checked_at DESC"
            rows = conn.run(sql, **params)
            return [self._row_to_health(r) for r in rows]

    def get_health_history(self, component: str, limit: int = 100) -> List[HealthCheck]:
        with self._conn() as conn:
            rows = conn.run("SELECT * FROM health_checks WHERE component = :component ORDER BY checked_at DESC LIMIT :limit", component=component, limit=limit)
            return [self._row_to_health(r) for r in rows]

    # System Logs
    def add_log(self, log: SystemLog) -> SystemLog:
        with self._conn() as conn:
            conn.run(
                """
                INSERT INTO system_logs (level, component, message, context, timestamp)
                VALUES (:level, :component, :message, :context, :timestamp)
                """,
                level=log.level,
                component=log.component,
                message=log.message,
                context=json.dumps(log.context),
                timestamp=log.timestamp,
            )
            return log

    def get_logs(self, level: Optional[str] = None, component: Optional[str] = None, since: Optional[str] = None, limit: int = 1000) -> List[SystemLog]:
        with self._conn() as conn:
            sql = "SELECT * FROM system_logs"
            params = {}
            conditions = []
            if level:
                conditions.append("level = :level")
                params["level"] = level
            if component:
                conditions.append("component = :component")
                params["component"] = component
            if since:
                conditions.append("timestamp >= :since")
                params["since"] = since
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY timestamp DESC LIMIT :limit"
            params["limit"] = limit
            rows = conn.run(sql, **params)
            return [self._row_to_log(r) for r in rows]

    # Aggregated stats
    def get_system_stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            # Node count
            nodes = conn.run("SELECT COUNT(*) FROM nodes WHERE status = 'online'")
            # Active alerts
            alerts = conn.run("SELECT COUNT(*) FROM alerts WHERE active = true")
            # Critical alerts
            critical = conn.run("SELECT COUNT(*) FROM alerts WHERE active = true AND severity = 'critical'")
            # Recent errors
            since = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            errors = conn.run("SELECT COUNT(*) FROM system_logs WHERE level IN ('error', 'critical') AND timestamp >= :since", since=since)
            # Health status
            health = conn.run("SELECT status, COUNT(*) FROM health_checks WHERE checked_at >= :since GROUP BY status", since=since)
            health_map = {row[0]: row[1] for row in health}
            return {
                "online_nodes": nodes[0][0] if nodes else 0,
                "active_alerts": alerts[0][0] if alerts else 0,
                "critical_alerts": critical[0][0] if critical else 0,
                "recent_errors": errors[0][0] if errors else 0,
                "health_summary": health_map,
            }

    def cleanup_old_data(self, retention_days: int = 30):
        with self._conn() as conn:
            cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
            conn.run("DELETE FROM metrics WHERE timestamp < :cutoff", cutoff=cutoff)
            conn.run("DELETE FROM health_checks WHERE checked_at < :cutoff", cutoff=cutoff)
            conn.run("DELETE FROM system_logs WHERE timestamp < :cutoff", cutoff=cutoff)
            # Keep alerts longer
            alert_cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
            conn.run("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < :cutoff", cutoff=alert_cutoff)


def migrate_existing_monitoring(db_url: str, legacy_data: Dict[str, Any]) -> Dict[str, int]:
    module = MonitoringModule(db_url)
    migrated = 0
    # Migrate alerts if any
    for alert_data in legacy_data.get("alerts", []):
        if not module.get_alert(alert_data["id"]):
            alert = Alert(**alert_data)
            module.create_alert(alert)
            migrated += 1
    return {"migrated": migrated, "skipped": 0}