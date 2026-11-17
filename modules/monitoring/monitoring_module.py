import os
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import pg8000.native
from monitoring_logic import MonitoringModule, Metric, Alert, HealthCheck, SystemLog, migrate_existing_monitoring

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vertep")
MODULE = MonitoringModule(DB_URL)


class MonitoringHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _json_response(self, data: Any, status: int = 200):
        self._set_headers(status)
        self.wfile.write(json.dumps(data, default=str).encode())

    def _parse_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/health":
                self._json_response({"status": "ok"})
            elif path == "/api/v1/metrics":
                name = query.get("name", [None])[0]
                node_id = query.get("node_id", [None])[0]
                since = query.get("since", [None])[0]
                limit = int(query.get("limit", ["1000"])[0])
                metrics = MODULE.get_metrics(name=name, node_id=node_id, since=since, limit=limit)
                self._json_response([m.__dict__ for m in metrics])
            elif path == "/api/v1/metrics/latest":
                node_id = query.get("node_id", [None])[0]
                metrics = MODULE.get_latest_metrics(node_id=node_id)
                self._json_response([m.__dict__ for m in metrics])
            elif path == "/api/v1/alerts":
                active_only = query.get("active_only", ["false"])[0].lower() == "true"
                severity = query.get("severity", [None])[0]
                alerts = MODULE.list_alerts(active_only=active_only, severity=severity)
                self._json_response([a.__dict__ for a in alerts])
            elif path.startswith("/api/v1/alerts/") and path.count("/") == 3:
                alert_id = path.split("/")[3]
                alert = MODULE.get_alert(alert_id)
                if alert:
                    self._json_response(alert.__dict__)
                else:
                    self._json_response({"error": "Alert not found"}, 404)
            elif path == "/api/v1/health":
                component = query.get("component", [None])[0]
                health = MODULE.get_latest_health(component=component)
                self._json_response([h.__dict__ for h in health])
            elif path.startswith("/api/v1/health/") and path.endswith("/history"):
                component = path.split("/")[3]
                limit = int(query.get("limit", ["100"])[0])
                history = MODULE.get_health_history(component, limit=limit)
                self._json_response([h.__dict__ for h in history])
            elif path == "/api/v1/logs":
                level = query.get("level", [None])[0]
                component = query.get("component", [None])[0]
                since = query.get("since", [None])[0]
                limit = int(query.get("limit", ["1000"])[0])
                logs = MODULE.get_logs(level=level, component=component, since=since, limit=limit)
                self._json_response([l.__dict__ for l in logs])
            elif path == "/api/v1/stats":
                stats = MODULE.get_system_stats()
                self._json_response(stats)
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            logger.exception("GET error")
            self._json_response({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()

        try:
            if path == "/api/v1/metrics":
                metric = Metric(
                    id=body.get("id", f"metric_{__import__('uuid').uuid4().hex[:12]}"),
                    name=body.get("name"),
                    value=float(body.get("value", 0)),
                    labels=body.get("labels", {}),
                    timestamp=body.get("timestamp") or __import__("datetime").datetime.utcnow().isoformat(),
                    node_id=body.get("node_id"),
                )
                created = MODULE.record_metric(metric)
                self._json_response(created.__dict__, 201)
            elif path == "/api/v1/alerts":
                alert = Alert(
                    id=body.get("id", f"alert_{__import__('uuid').uuid4().hex[:12]}"),
                    name=body.get("name"),
                    severity=body.get("severity", "info"),
                    condition=body.get("condition", ""),
                    message=body.get("message", ""),
                    active=body.get("active", False),
                    fired_at=body.get("fired_at"),
                    resolved_at=body.get("resolved_at"),
                    labels=body.get("labels", {}),
                )
                created = MODULE.create_alert(alert)
                self._json_response(created.__dict__, 201)
            elif path.startswith("/api/v1/alerts/") and path.endswith("/fire"):
                alert_id = path.split("/")[3]
                message = body.get("message", "Alert fired")
                alert = MODULE.fire_alert(alert_id, message)
                if alert:
                    self._json_response(alert.__dict__)
                else:
                    self._json_response({"error": "Alert not found"}, 404)
            elif path.startswith("/api/v1/alerts/") and path.endswith("/resolve"):
                alert_id = path.split("/")[3]
                alert = MODULE.resolve_alert(alert_id)
                if alert:
                    self._json_response(alert.__dict__)
                else:
                    self._json_response({"error": "Alert not found"}, 404)
            elif path == "/api/v1/health":
                health = HealthCheck(
                    id=body.get("id", f"health_{__import__('uuid').uuid4().hex[:12]}"),
                    component=body.get("component"),
                    status=body.get("status", "healthy"),
                    message=body.get("message", ""),
                    details=body.get("details", {}),
                    checked_at=body.get("checked_at") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.record_health_check(health)
                self._json_response(created.__dict__, 201)
            elif path == "/api/v1/logs":
                log = SystemLog(
                    id=0,
                    level=body.get("level", "info"),
                    component=body.get("component"),
                    message=body.get("message"),
                    context=body.get("context", {}),
                    timestamp=body.get("timestamp") or __import__("datetime").datetime.utcnow().isoformat(),
                )
                created = MODULE.add_log(log)
                self._json_response({"id": "logged"}, 201)
            elif path == "/api/v1/cleanup":
                retention_days = int(body.get("retention_days", 30))
                MODULE.cleanup_old_data(retention_days)
                self._json_response({"cleaned": True})
            elif path == "/api/v1/migrate":
                result = migrate_existing_monitoring(DB_URL, body.get("data", {}))
                self._json_response(result)
            else:
                self._json_response({"error": "Not found"}, 404)
        except ValueError as e:
            self._json_response({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("POST error")
            self._json_response({"error": str(e)}, 500)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._parse_body()

        try:
            if path.startswith("/api/v1/alerts/") and path.count("/") == 3:
                alert_id = path.split("/")[3]
                alert = MODULE.update_alert(alert_id, body)
                if alert:
                    self._json_response(alert.__dict__)
                else:
                    self._json_response({"error": "Alert not found"}, 404)
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            logger.exception("PATCH error")
            self._json_response({"error": str(e)}, 500)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def main():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), MonitoringHandler)
    logger.info(f"Monitoring module listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()