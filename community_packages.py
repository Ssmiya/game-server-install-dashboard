from __future__ import annotations

import json
import re
import shutil
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 24 * 1024 * 1024
MAX_FILES = 8
GAME_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
ALLOWED_TYPES = {"text", "password", "number", "boolean", "range", "select"}
ALLOWED_ASSET_SUFFIXES = {".jpg", ".jpeg", ".png"}


class PackageValidationError(ValueError):
    pass


class CommunityPackageStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()

    @staticmethod
    def _safe_relative(value: str, label: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or any(part in {"", "."} for part in path.parts)
        ):
            raise PackageValidationError(f"{label} 必须是安全的相对路径")
        return str(path)

    @staticmethod
    def _validate_image(data: bytes, suffix: str, label: str) -> None:
        suffix = suffix.lower()
        width = height = 0
        if suffix == ".png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
        elif suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8"):
            position = 2
            while position + 9 < len(data):
                if data[position] != 0xFF:
                    position += 1
                    continue
                marker = data[position + 1]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    height = int.from_bytes(data[position + 5:position + 7], "big")
                    width = int.from_bytes(data[position + 7:position + 9], "big")
                    break
                if position + 4 > len(data):
                    break
                segment_length = int.from_bytes(data[position + 2:position + 4], "big")
                if segment_length < 2:
                    break
                position += 2 + segment_length
        if not width or not height:
            raise PackageValidationError(f"{label} 文件内容与扩展名不匹配")
        if width > 8192 or height > 8192 or width * height > 32_000_000:
            raise PackageValidationError(f"{label} 图片尺寸过大")

    @classmethod
    def validate_manifest(cls, manifest: Any, archive_names: set[str]) -> dict[str, Any]:
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
            raise PackageValidationError("game.json 的 schemaVersion 必须为 1")
        game_id = str(manifest.get("id", ""))
        if not GAME_ID_PATTERN.fullmatch(game_id):
            raise PackageValidationError("游戏 ID 必须为 3-32 位小写字母、数字或连字符")

        def text_value(key: str, maximum: int, required: bool = True) -> str:
            value = str(manifest.get(key, "")).strip()
            if required and not value:
                raise PackageValidationError(f"{key} 不能为空")
            if len(value) > maximum:
                raise PackageValidationError(f"{key} 最长 {maximum} 个字符")
            return value

        name = text_value("name", 60)
        short_name = text_value("shortName", 8)
        description = text_value("description", 300)
        accent = text_value("accent", 7)
        if not HEX_COLOR_PATTERN.fullmatch(accent):
            raise PackageValidationError("accent 必须是 #RRGGBB 颜色")
        tags = manifest.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 8 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 20 for tag in tags):
            raise PackageValidationError("tags 最多 8 项，每项不超过 20 个字符")

        assets = manifest.get("assets")
        if not isinstance(assets, dict):
            raise PackageValidationError("缺少 assets 配置")
        icon = cls._safe_relative(str(assets.get("icon", "")), "assets.icon")
        background = cls._safe_relative(str(assets.get("background", "")), "assets.background")
        for filename, label in ((icon, "图标"), (background, "背景图")):
            if "/" in filename or Path(filename).suffix.lower() not in ALLOWED_ASSET_SUFFIXES:
                raise PackageValidationError(f"{label}必须是 ZIP 根目录下的 JPG 或 PNG")
            if filename not in archive_names:
                raise PackageValidationError(f"ZIP 中缺少 {filename}")

        adapter = manifest.get("adapter")
        if not isinstance(adapter, dict) or adapter.get("type") != "steamcmd":
            raise PackageValidationError("当前只允许 steamcmd 适配器")
        app_id = adapter.get("appId")
        if not isinstance(app_id, int) or not 1 <= app_id <= 2_147_483_647:
            raise PackageValidationError("adapter.appId 必须是有效的 Steam App ID")
        executable = cls._safe_relative(str(adapter.get("executable", "")), "adapter.executable")
        config_name = cls._safe_relative(str(adapter.get("configName", "")), "adapter.configName")
        library_path = str(adapter.get("libraryPath", "")).strip()
        if library_path:
            library_path = cls._safe_relative(library_path, "adapter.libraryPath")
        if not config_name.endswith((".cfg", ".conf", ".ini", ".properties", ".txt")):
            raise PackageValidationError("配置文件扩展名不受支持")
        launch_args = adapter.get("launchArgs", [])
        if (
            not isinstance(launch_args, list)
            or len(launch_args) > 64
            or any(not isinstance(arg, str) or len(arg) > 240 or "\x00" in arg for arg in launch_args)
        ):
            raise PackageValidationError("launchArgs 必须是最多 64 项的字符串数组")

        fields = manifest.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 100:
            raise PackageValidationError("fields 必须是最多 100 项的数组")
        validated_fields: list[dict[str, Any]] = []
        keys: set[str] = set()
        for index, field in enumerate(fields):
            if not isinstance(field, dict):
                raise PackageValidationError(f"fields[{index}] 格式错误")
            key = str(field.get("key", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key) or key in keys:
                raise PackageValidationError(f"fields[{index}].key 无效或重复")
            field_type = field.get("type")
            if field_type not in ALLOWED_TYPES:
                raise PackageValidationError(f"fields[{index}].type 不受支持")
            item = {
                "key": key,
                "label": str(field.get("label", key))[:80],
                "type": field_type,
                "value": field.get("value", False if field_type == "boolean" else ""),
                "group": str(field.get("group", "基础设置"))[:40],
                "description": str(field.get("description", ""))[:240],
            }
            if field_type in {"number", "range"}:
                for option in ("min", "max", "step"):
                    if option in field and isinstance(field[option], (int, float)):
                        item[option] = field[option]
            if field_type == "select":
                options = field.get("options", [])
                if not isinstance(options, list) or not options or len(options) > 50:
                    raise PackageValidationError(f"fields[{index}].options 无效")
                item["options"] = [str(option)[:80] for option in options]
            validated_fields.append(item)
            keys.add(key)

        default_config = manifest.get("defaultConfig", "")
        if not isinstance(default_config, str) or len(default_config.encode("utf-8")) > 128 * 1024:
            raise PackageValidationError("defaultConfig 最大为 128KB")
        capacity_field = str(adapter.get("capacityField", ""))
        port_field = str(adapter.get("portField", ""))
        for value, label in ((capacity_field, "capacityField"), (port_field, "portField")):
            if value and value not in keys:
                raise PackageValidationError(f"adapter.{label} 必须引用 fields 中的键")

        return {
            "schemaVersion": 1,
            "id": game_id,
            "name": name,
            "shortName": short_name,
            "description": description,
            "accent": accent,
            "tags": [tag.strip() for tag in tags],
            "assets": {"icon": icon, "background": background},
            "adapter": {
                "type": "steamcmd",
                "appId": app_id,
                "executable": executable,
                "configName": config_name,
                "launchArgs": launch_args,
                "libraryPath": library_path,
                "portField": port_field,
                "capacityField": capacity_field,
            },
            "fields": validated_fields,
            "defaultConfig": default_config,
        }

    def import_zip(self, archive_path: Path) -> dict[str, Any]:
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise PackageValidationError("适配包不能超过 10MB")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if len(infos) > MAX_FILES:
                    raise PackageValidationError("适配包文件数量不能超过 8 个")
                if sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
                    raise PackageValidationError("适配包解压后不能超过 24MB")
                names: set[str] = set()
                for item in infos:
                    normalized = self._safe_relative(item.filename, "ZIP 文件路径")
                    if "/" in normalized:
                        raise PackageValidationError("适配包只允许根目录文件")
                    if normalized in names:
                        raise PackageValidationError("ZIP 中存在重复文件名")
                    names.add(normalized)
                if "game.json" not in names:
                    raise PackageValidationError("ZIP 中缺少 game.json")
                try:
                    manifest = json.loads(archive.read("game.json").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PackageValidationError("game.json 不是有效的 UTF-8 JSON") from exc
                manifest = self.validate_manifest(manifest, names)
                allowed_names = {
                    "game.json",
                    manifest["assets"]["icon"],
                    manifest["assets"]["background"],
                }
                unexpected = names - allowed_names
                if unexpected:
                    raise PackageValidationError(
                        f"适配包包含不允许的文件：{sorted(unexpected)[0]}"
                    )
                icon_data = archive.read(manifest["assets"]["icon"])
                background_data = archive.read(manifest["assets"]["background"])
                self._validate_image(icon_data, Path(manifest["assets"]["icon"]).suffix, "图标")
                self._validate_image(background_data, Path(manifest["assets"]["background"]).suffix, "背景图")
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise PackageValidationError("上传文件不是有效或可读取的 ZIP") from exc

        game_id = manifest["id"]
        target = self.root / game_id
        temporary = self.root / f".{game_id}.uploading"
        with self.lock:
            if target.exists():
                raise PackageValidationError("该游戏 ID 已存在")
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(parents=True, exist_ok=False)
            (temporary / "game.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (temporary / manifest["assets"]["icon"]).write_bytes(icon_data)
            (temporary / manifest["assets"]["background"]).write_bytes(background_data)
            (temporary / "status.json").write_text(
                json.dumps({"status": "pending"}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(target)
        return {**manifest, "status": "pending"}

    def list_packages(self) -> list[dict[str, Any]]:
        result = []
        if not self.root.exists():
            return result
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            try:
                manifest = json.loads((folder / "game.json").read_text(encoding="utf-8"))
                status = json.loads((folder / "status.json").read_text(encoding="utf-8")).get("status")
                if status not in {"pending", "approved"}:
                    continue
                result.append({**manifest, "status": status})
            except (OSError, ValueError, TypeError):
                continue
        return result

    def set_status(self, game_id: str, status: str) -> None:
        if not GAME_ID_PATTERN.fullmatch(game_id) or status not in {"pending", "approved"}:
            raise PackageValidationError("无效的适配包状态")
        folder = self.root / game_id
        if not (folder / "game.json").is_file():
            raise FileNotFoundError(game_id)
        temporary = folder / "status.tmp"
        temporary.write_text(json.dumps({"status": status}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(folder / "status.json")

    def delete(self, game_id: str) -> None:
        if not GAME_ID_PATTERN.fullmatch(game_id):
            raise PackageValidationError("无效的游戏 ID")
        folder = self.root / game_id
        if not folder.is_dir():
            raise FileNotFoundError(game_id)
        shutil.rmtree(folder)
