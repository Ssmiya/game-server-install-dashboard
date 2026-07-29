from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.security import check_password_hash

from game_fields import MINECRAFT_FIELDS, PALWORLD_FIELDS
from production_runtime import GameRuntime, JobStore


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GAME_DASHBOARD_DATA", BASE_DIR / "data"))
EXECUTE_COMMANDS = os.environ.get("GAME_DASHBOARD_EXECUTE", "0") == "1"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
runtime = GameRuntime(EXECUTE_COMMANDS, DATA_DIR)
job_store = JobStore(DATA_DIR / "jobs.sqlite3")


def authentication_enabled() -> bool:
    return bool(os.environ.get("DASHBOARD_PASSWORD_HASH"))


@app.before_request
def protect_dashboard():
    if request.path == "/health":
        return None
    if authentication_enabled():
        auth = request.authorization
        expected_user = os.environ.get("DASHBOARD_USERNAME", "admin")
        password_hash = os.environ["DASHBOARD_PASSWORD_HASH"]
        if (
            not auth
            or not secrets.compare_digest(auth.username or "", expected_user)
            or not check_password_hash(password_hash, auth.password or "")
        ):
            return Response(
                "需要身份验证",
                401,
                {"WWW-Authenticate": 'Basic realm="GameDeck", charset="UTF-8"'},
            )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = os.environ.get("DASHBOARD_CSRF_TOKEN")
        if expected and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), expected):
            return jsonify({"error": "请求验证失败，请刷新页面后重试"}), 403
    return None


@dataclass(frozen=True)
class GameDefinition:
    id: str
    name: str
    short_name: str
    eyebrow: str
    accent: str
    config_name: str
    service_name: str
    version: str
    latest_version: str
    fields: tuple[dict[str, Any], ...]
    default_config: str


GAMES: dict[str, GameDefinition] = {
    "palworld": GameDefinition(
        id="palworld",
        name="幻兽帕鲁",
        short_name="PAL",
        eyebrow="PALWORLD DEDICATED SERVER",
        accent="#8b7cff",
        config_name="PalWorldSettings.ini",
        service_name="palworld.service",
        version="演示版本 0.6.8",
        latest_version="演示最新版本",
        fields=tuple(PALWORLD_FIELDS),
        default_config=(
            "[/Script/Pal.PalGameWorldSettings]\n"
            "OptionSettings=(ServerName=\"Pal Haven\",ServerDescription=\"A private world for friends.\","
            "AdminPassword=\"\",ServerPassword=\"\",PublicPort=8211,ServerPlayerMaxNum=16,"
            "RESTAPIEnabled=False,RESTAPIPort=8212,RCONEnabled=False,RCONPort=25575,"
            "CrossplayPlatforms=(Steam,Xbox,PS5,Mac),LogFormatType=\"Text\")\n"
        ),
    ),
    "minecraft": GameDefinition(
        id="minecraft",
        name="Minecraft Java",
        short_name="MC",
        eyebrow="MINECRAFT JAVA SERVER",
        accent="#72d572",
        config_name="server.properties",
        service_name="minecraft.service",
        version="演示版本 1.21",
        latest_version="演示最新版本",
        fields=tuple(MINECRAFT_FIELDS),
        default_config=(
            "motd=A cozy Minecraft server\nserver-port=25565\nmax-players=12\n"
            "difficulty=normal\ngamemode=survival\nview-distance=10\nsimulation-distance=10\n"
            "pvp=true\nonline-mode=true\nwhite-list=false\n"
        ),
    ),
}


def public_game(game: GameDefinition, include_status: bool = True) -> dict[str, Any]:
    values = runtime.read_values(game)
    fields = []
    for definition in game.fields:
        value = values.get(definition["key"], definition["value"])
        item = {**definition}
        if definition["type"] == "password":
            item["value"] = ""
            item["hasValue"] = bool(value)
        else:
            item["value"] = value
        fields.append(item)
    status = runtime.status(game, values) if include_status else {}
    return {
        "id": game.id,
        "name": game.name,
        "shortName": game.short_name,
        "eyebrow": game.eyebrow,
        "accent": game.accent,
        "installDir": str(runtime.install_dir(game.id)),
        "configName": str(runtime.config_path(game)),
        "serviceName": game.service_name,
        "version": status.get("version", game.version),
        "latestVersion": status.get("latestVersion", game.latest_version),
        "fields": fields,
        "state": status,
    }


def run_job(job_id: str, game_id: str, action: str, accept_eula: bool = False) -> None:
    game = GAMES[game_id]
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        job_store.update(job_id, logs=logs, message=message, progress=min(90, 8 + len(logs) * 4))

    try:
        if EXECUTE_COMMANDS:
            runtime.perform_action(game, action, log, accept_eula)
        else:
            messages = {
                "install": ["检查运行环境", "准备安装目录", "下载服务端文件", "校验文件完整性", "安装完成"],
                "update": ["停止游戏服务", "检查最新版本", "下载更新", "校验服务端文件", "更新完成"],
                "start": ["读取启动参数", "启动游戏进程", "服务已启动"],
                "stop": ["发送停止信号", "等待进程退出", "服务已停止"],
                "restart": ["停止游戏服务", "载入最新配置", "重新启动", "服务已重新启动"],
            }
            for index, message in enumerate(messages[action], 1):
                log(message)
                job_store.update(job_id, progress=round(index / len(messages[action]) * 100))
                time.sleep(0.5)
        job_store.update(job_id, status="done", progress=100, message="操作已完成", logs=logs)
    except Exception as exc:
        logs.append(f"[错误] {exc}")
        job_store.update(job_id, status="failed", message=str(exc), logs=logs)


@app.get("/")
def index():
    return render_template(
        "index.html",
        demo_mode=not EXECUTE_COMMANDS,
        csrf_token=os.environ.get("DASHBOARD_CSRF_TOKEN", ""),
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok", "mode": "production" if EXECUTE_COMMANDS else "demo"})


@app.get("/api/games")
def game_list():
    return jsonify([public_game(game) for game in GAMES.values()])


@app.get("/api/games/<game_id>/status")
def game_status(game_id: str):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"error": "游戏不存在"}), 404
    values = runtime.read_values(game)
    return jsonify(runtime.status(game, values))


@app.post("/api/games/<game_id>/actions/<action>")
def game_action(game_id: str, action: str):
    if game_id not in GAMES or action not in {"install", "update", "start", "stop", "restart"}:
        return jsonify({"error": "不支持的操作"}), 404
    job_id = uuid.uuid4().hex
    job = job_store.create(job_id, game_id, action)
    payload = request.get_json(silent=True) or {}
    accept_eula = bool(payload.get("acceptEula")) if game_id == "minecraft" else False
    threading.Thread(
        target=run_job,
        args=(job_id, game_id, action, accept_eula),
        daemon=True,
    ).start()
    return jsonify(job), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@app.get("/api/games/<game_id>/config/raw")
def get_raw_config(game_id: str):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"error": "游戏不存在"}), 404
    path = runtime.ensure_config(game)
    return jsonify({"content": path.read_text(encoding="utf-8", errors="replace"), "filename": str(path)})


@app.put("/api/games/<game_id>/config/raw")
def save_raw_config(game_id: str):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"error": "游戏不存在"}), 404
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("content"), str) or len(payload["content"]) > 2_000_000:
        return jsonify({"error": "配置内容无效或过大"}), 400
    path = runtime.ensure_config(game)
    runtime.backup(game)
    path.write_text(payload["content"], encoding="utf-8")
    return jsonify({"ok": True, "message": "原始配置已保存"})


@app.put("/api/games/<game_id>/config/form")
def save_form_config(game_id: str):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"error": "游戏不存在"}), 404
    payload = request.get_json(silent=True) or {}
    definitions = {field["key"]: field for field in game.fields}
    values: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in definitions:
            continue
        definition = definitions[key]
        if definition["type"] == "boolean" and not isinstance(value, bool):
            return jsonify({"error": f"{key} 必须是布尔值"}), 400
        if definition["type"] == "number":
            if not isinstance(value, (int, float)):
                return jsonify({"error": f"{key} 必须是数字"}), 400
            if "min" in definition and value < definition["min"]:
                return jsonify({"error": f"{key} 不能小于 {definition['min']}"}), 400
            if "max" in definition and value > definition["max"]:
                return jsonify({"error": f"{key} 不能大于 {definition['max']}"}), 400
        if definition["type"] == "select" and value not in definition["options"]:
            return jsonify({"error": f"{key} 的值不受支持"}), 400
        values[key] = value
    runtime.write_values(game, values)
    return jsonify({"ok": True, "message": "参数已写入游戏配置"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=not EXECUTE_COMMANDS)
