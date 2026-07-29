from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psutil


LogWriter = Callable[[str], None]


class JobStore:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.lock = threading.Lock()
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    logs TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "UPDATE jobs SET status='failed', message='Dashboard 重启，任务已中断' WHERE status='running'"
            )

    def connect(self):
        return sqlite3.connect(self.database, timeout=10)

    def create(self, job_id: str, game_id: str, action: str) -> dict[str, Any]:
        now = int(time.time())
        with self.lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, 'running', 0, '任务已创建', '[]', ?, ?)",
                (job_id, game_id, action, now, now),
            )
        return self.get(job_id)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            return self.get(job_id)
        allowed = {"status", "progress", "message", "logs"}
        changes = {key: value for key, value in changes.items() if key in allowed}
        if "logs" in changes:
            changes["logs"] = json.dumps(changes["logs"], ensure_ascii=False)
        changes["updated_at"] = int(time.time())
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.lock, self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*changes.values(), job_id),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, action, status, progress, message, logs, created_at, updated_at FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "gameId": row[1], "action": row[2], "status": row[3],
            "progress": row[4], "message": row[5], "logs": json.loads(row[6]),
            "createdAt": row[7], "updatedAt": row[8],
        }


class GameRuntime:
    def __init__(self, execute: bool, data_root: Path):
        self.execute = execute
        self.data_root = data_root
        self.game_root = Path(os.environ.get("GAME_ROOT", "/srv/games"))
        self.steamcmd = Path(os.environ.get("STEAMCMD_PATH", "/opt/steamcmd/steamcmd.sh"))
        self.action_locks = {"palworld": threading.Lock(), "minecraft": threading.Lock()}
        self.steamcmd_lock = threading.Lock()
        self.latest_cache: dict[str, tuple[float, str]] = {}

    def install_dir(self, game_id: str) -> Path:
        return self.game_root / game_id / "server" if self.execute else self.data_root / game_id

    def backup_dir(self, game_id: str) -> Path:
        return self.game_root / game_id / "backups" if self.execute else self.data_root / game_id / "backups"

    def config_path(self, game: Any) -> Path:
        if not self.execute:
            return self.data_root / game.id / game.config_name
        if game.id == "palworld":
            return self.install_dir(game.id) / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
        return self.install_dir(game.id) / "server.properties"

    def ensure_config(self, game: Any) -> Path:
        path = self.config_path(game)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(game.default_config, encoding="utf-8")
        return path

    def backup(self, game: Any) -> None:
        path = self.config_path(game)
        if not path.exists():
            return
        target_dir = self.backup_dir(game.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, target_dir / f"{path.name}.{stamp}.backup")
        backups = sorted(target_dir.glob(f"{path.name}.*.backup"), reverse=True)
        for old in backups[20:]:
            old.unlink(missing_ok=True)

    @staticmethod
    def split_palworld(content: str) -> dict[str, str]:
        marker = "OptionSettings=("
        if marker not in content:
            return {}
        body = content.split(marker, 1)[1].rsplit(")", 1)[0]
        parts, current, depth, quoted = [], [], 0, False
        for char in body:
            if char == '"':
                quoted = not quoted
            elif not quoted and char == "(":
                depth += 1
            elif not quoted and char == ")":
                depth = max(0, depth - 1)
            if char == "," and not quoted and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(char)
        if current:
            parts.append("".join(current))
        return {part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip() for part in parts if "=" in part}

    @staticmethod
    def decode_value(raw: str, field: dict[str, Any]) -> Any:
        raw = raw.strip()
        if field["type"] == "boolean":
            return raw.lower() == "true"
        if field["type"] == "number":
            try:
                return float(raw) if "." in raw else int(raw)
            except ValueError:
                return field["value"]
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            return raw[1:-1]
        return raw

    def read_values(self, game: Any) -> dict[str, Any]:
        path = self.ensure_config(game)
        content = path.read_text(encoding="utf-8", errors="replace")
        definitions = {item["key"]: item for item in game.fields}
        if game.id == "palworld":
            raw_values = self.split_palworld(content)
        else:
            raw_values = {}
            for line in content.splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    raw_values[key.strip()] = value.strip()
            runtime_path = self.game_root / "minecraft/config/runtime.env"
            if self.execute and runtime_path.exists():
                runtime = {
                    key: value for key, value in (
                        line.split("=", 1) for line in runtime_path.read_text().splitlines()
                        if "=" in line and not line.startswith("#")
                    )
                }
                raw_values["memory"] = runtime.get("JAVA_XMX", "6G").rstrip("Gg")
                raw_values["initial-memory"] = runtime.get("JAVA_XMS", "2G").rstrip("Gg")
                raw_values["nogui"] = "true"
        return {
            key: self.decode_value(raw, definitions[key])
            for key, raw in raw_values.items() if key in definitions
        }

    @staticmethod
    def format_pal_value(field: dict[str, Any], value: Any) -> str:
        if field["type"] == "boolean":
            return "True" if value else "False"
        if field["type"] == "number":
            return str(value)
        if field["key"] in {"CrossplayPlatforms", "DenyTechnologyList"} and str(value).strip().startswith("("):
            return str(value).strip()
        return f'"{str(value).replace(chr(34), "")}"'

    def write_values(self, game: Any, incoming: dict[str, Any]) -> None:
        path = self.ensure_config(game)
        self.backup(game)
        definitions = {item["key"]: item for item in game.fields}
        current = self.read_values(game)
        for key, value in incoming.items():
            field = definitions[key]
            if field["type"] == "password" and value == "":
                continue
            current[key] = value

        if game.id == "palworld":
            existing = self.split_palworld(path.read_text(encoding="utf-8", errors="replace"))
            for key, value in current.items():
                existing[key] = self.format_pal_value(definitions[key], value)
            rendered = ",".join(f"{key}={value}" for key, value in existing.items())
            path.write_text(f"[/Script/Pal.PalGameWorldSettings]\nOptionSettings=({rendered})\n", encoding="utf-8")
            return

        runtime_keys = {"memory", "initial-memory", "nogui"}
        properties = {key: value for key, value in current.items() if key not in runtime_keys}
        existing_lines, written = [], set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
            if key in properties:
                value = properties[key]
                existing_lines.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
                written.add(key)
            else:
                existing_lines.append(line)
        for key, value in properties.items():
            if key not in written:
                existing_lines.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
        if self.execute:
            runtime_path = self.game_root / "minecraft/config/runtime.env"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text(
                f"JAVA_XMS={current.get('initial-memory', 2)}G\nJAVA_XMX={current.get('memory', 6)}G\n",
                encoding="utf-8",
            )

    def run_command(self, command: list[str], log: LogWriter, cwd: Path | None = None, timeout: int = 1800) -> None:
        log("$ " + " ".join(command))
        process = subprocess.Popen(
            command, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        started = time.monotonic()
        assert process.stdout is not None
        for line in process.stdout:
            log(line.rstrip())
            if time.monotonic() - started > timeout:
                process.kill()
                raise RuntimeError("操作超时")
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"命令执行失败，退出码 {code}")

    def install_palworld(self, game: Any, log: LogWriter) -> None:
        directory = self.install_dir(game.id)
        directory.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.steamcmd),
            "+@sSteamCmdForcePlatformType", "linux",
            "+force_install_dir", str(directory),
            "+login", "anonymous",
            "+app_info_update", "1",
            "+app_update", "2394010", "validate",
            "+quit",
        ]
        with self.steamcmd_lock:
            try:
                self.run_command(command, log, cwd=self.steamcmd.parent)
            except RuntimeError:
                log("SteamCMD 配置缓存无效，正在清理元数据缓存后重试")
                cache_candidates = [
                    self.steamcmd.parent / "appcache",
                    Path.home() / ".steam/appcache",
                    Path.home() / ".steam/steam/appcache",
                ]
                for cache in cache_candidates:
                    if cache.exists():
                        shutil.rmtree(cache)
                time.sleep(2)
                self.run_command(command, log, cwd=self.steamcmd.parent)
        default = directory / "DefaultPalWorldSettings.ini"
        config = self.config_path(game)
        config.parent.mkdir(parents=True, exist_ok=True)
        if default.exists() and not config.exists():
            shutil.copy2(default, config)
        steamclient = self.steamcmd.parent / "linux64/steamclient.so"
        if steamclient.exists():
            sdk_dir = Path.home() / ".steam/sdk64"
            sdk_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(steamclient, sdk_dir / "steamclient.so")
            log("Steam Linux 运行库已准备完成")

    @staticmethod
    def fetch_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "GameDeck/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def install_minecraft(self, game: Any, log: LogWriter, accept_eula: bool = False) -> None:
        eula_path = self.install_dir(game.id) / "eula.txt"
        already_accepted = eula_path.exists() and "eula=true" in eula_path.read_text(
            encoding="utf-8", errors="replace"
        ).lower()
        if not already_accepted and not accept_eula:
            raise RuntimeError("安装 Minecraft 前必须在页面确认接受 Minecraft EULA")
        log("正在查询 Mojang 官方版本清单")
        manifest = self.fetch_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        version_id = os.environ.get("MINECRAFT_VERSION", manifest["latest"]["release"])
        version_entry = next((item for item in manifest["versions"] if item["id"] == version_id), None)
        if not version_entry:
            raise RuntimeError(f"找不到 Minecraft 版本：{version_id}")
        details = self.fetch_json(version_entry["url"])
        download = details.get("downloads", {}).get("server")
        if not download:
            raise RuntimeError(f"Minecraft {version_id} 没有官方服务端文件")
        directory = self.install_dir(game.id)
        directory.mkdir(parents=True, exist_ok=True)
        target, temporary = directory / "server.jar", directory / "server.jar.download"
        log(f"正在下载 Minecraft Java Server {version_id}")
        request = urllib.request.Request(download["url"], headers={"User-Agent": "GameDeck/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha1(temporary.read_bytes()).hexdigest()
        if digest.lower() != download["sha1"].lower():
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Minecraft 服务端 SHA-1 校验失败")
        temporary.replace(target)
        (directory / ".version.json").write_text(
            json.dumps({"id": version_id, "sha1": digest, "installedAt": int(time.time())}, indent=2),
            encoding="utf-8",
        )
        (directory / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        self.ensure_config(game)
        log(f"Minecraft {version_id} 安装完成，SHA-1 校验通过")

    def perform_action(self, game: Any, action: str, log: LogWriter, accept_eula: bool = False) -> None:
        lock = self.action_locks[game.id]
        if not lock.acquire(blocking=False):
            raise RuntimeError("该游戏已有任务正在执行")
        try:
            if action in {"install", "update"}:
                if self.is_running(game.id):
                    self.systemctl(game.id, "stop", log)
                if game.id == "palworld":
                    self.install_palworld(game, log)
                else:
                    self.install_minecraft(game, log, accept_eula)
            else:
                self.systemctl(game.id, action, log)
        finally:
            lock.release()

    @staticmethod
    def systemctl(game_id: str, action: str, log: LogWriter) -> None:
        command = ["sudo", "-n", "/usr/bin/systemctl", action, f"{game_id}.service"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"{game_id}.service 执行 {action} 失败")
        log(f"{game_id}.service：{action} 完成")

    @staticmethod
    def is_running(game_id: str) -> bool:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", f"{game_id}.service"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "active"

    def process_stats(self, game_id: str) -> dict[str, Any]:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", f"{game_id}.service", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            pid = 0
        if pid <= 0:
            return {"cpu": 0, "memory": 0, "uptime": "—"}
        try:
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
            memory = sum(item.memory_info().rss for item in processes if item.is_running()) / 1024 ** 3
            cpu = sum(item.cpu_percent(interval=0.05) for item in processes if item.is_running())
            seconds = max(0, int(time.time() - root.create_time()))
            days, remainder = divmod(seconds, 86400)
            hours, _ = divmod(remainder, 3600)
            uptime = f"{days}天 {hours}小时" if days else f"{hours}小时"
            return {"cpu": round(cpu, 1), "memory": round(memory, 1), "uptime": uptime}
        except (psutil.Error, OSError):
            return {"cpu": 0, "memory": 0, "uptime": "—"}

    def player_count(self, game: Any, values: dict[str, Any]) -> int:
        try:
            if game.id == "minecraft":
                from mcstatus import JavaServer
                host = os.environ.get("MINECRAFT_QUERY_HOST", "127.0.0.1")
                port = int(values.get("server-port", 25565))
                return JavaServer.lookup(f"{host}:{port}", timeout=2).status().players.online
            if not values.get("RESTAPIEnabled"):
                return 0
            port = int(values.get("RESTAPIPort", 8212))
            password = values.get("AdminPassword", "")
            token = base64.b64encode(f"admin:{password}".encode()).decode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/api/players",
                headers={"Authorization": f"Basic {token}", "User-Agent": "GameDeck/1.0"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.load(response)
            players = payload.get("players", payload if isinstance(payload, list) else [])
            return len(players)
        except Exception:
            return 0

    def installed_version(self, game_id: str) -> str:
        if game_id == "minecraft":
            path = self.install_dir(game_id) / ".version.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")).get("id", "未知")
            return "未安装"
        manifest = self.install_dir(game_id).parent / "steamapps/appmanifest_2394010.acf"
        candidates = [
            manifest,
            self.install_dir(game_id) / "steamapps/appmanifest_2394010.acf",
            self.install_dir(game_id).parent / "appmanifest_2394010.acf",
        ]
        for path in candidates:
            if path.exists():
                match = re.search(r'"buildid"\s+"(\d+)"', path.read_text(errors="replace"))
                if match:
                    return f"Build {match.group(1)}"
        return "已安装" if (self.install_dir(game_id) / "PalServer.sh").exists() else "未安装"

    def _refresh_latest_version(self, game_id: str) -> None:
        try:
            if game_id == "minecraft":
                value = self.fetch_json(
                    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
                )["latest"]["release"]
            else:
                with self.steamcmd_lock:
                    result = subprocess.run(
                        [str(self.steamcmd), "+@sSteamCmdForcePlatformType", "linux",
                         "+login", "anonymous", "+app_info_update", "1",
                         "+app_info_print", "2394010", "+quit"],
                        cwd=str(self.steamcmd.parent),
                        capture_output=True, text=True, timeout=120,
                    )
                public = re.search(r'"public".{0,600}?"buildid"\s+"(\d+)"', result.stdout, re.S)
                value = f"Build {public.group(1)}" if public else "检查失败"
        except Exception:
            value = "检查失败"
        self.latest_cache[game_id] = (time.time(), value)

    def latest_version(self, game_id: str) -> str:
        cached = self.latest_cache.get(game_id)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]
        self.latest_cache[game_id] = (time.time(), "检查中")
        threading.Thread(target=self._refresh_latest_version, args=(game_id,), daemon=True).start()
        return "检查中"
        return value

    def status(self, game: Any, values: dict[str, Any]) -> dict[str, Any]:
        if not self.execute:
            samples = {
                "palworld": {"installed": True, "running": True, "players": 3, "capacity": 16, "uptime": "2天 14小时", "cpu": 18, "memory": 5.7},
                "minecraft": {"installed": True, "running": False, "players": 0, "capacity": 12, "uptime": "—", "cpu": 0, "memory": 0},
            }
            return {**samples[game.id], "version": game.version, "latestVersion": game.latest_version}
        running = self.is_running(game.id)
        stats = self.process_stats(game.id) if running else {"cpu": 0, "memory": 0, "uptime": "—"}
        capacity = int(values.get("ServerPlayerMaxNum" if game.id == "palworld" else "max-players", 0))
        return {
            "installed": self.installed_version(game.id) != "未安装",
            "running": running,
            "players": self.player_count(game, values) if running else 0,
            "capacity": capacity,
            **stats,
            "version": self.installed_version(game.id),
            "latestVersion": self.latest_version(game.id),
        }
